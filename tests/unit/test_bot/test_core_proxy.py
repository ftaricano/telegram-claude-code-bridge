"""Tests for proxy wiring and credential redaction in bot core.

Adapted from upstream RichardAtCT/claude-code-telegram PR #218 (MIT).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

import src.bot.core as core_module
from src.bot.core import ClaudeCodeBot
from src.config import create_test_config

PROXY_URL = "http://alice:s3cret@proxy.example.com:3128"
REDACTED_PROXY_URL = "http://alice:***@proxy.example.com:3128"


@pytest.fixture
def bot_with_builder(monkeypatch):
    """Create a bot with mocked Application builder plumbing."""
    settings = create_test_config()
    deps = {
        "storage": MagicMock(),
        "security": MagicMock(),
    }
    bot = ClaudeCodeBot(settings, deps)

    builder = MagicMock()

    app = MagicMock()
    app.bot = MagicMock()
    app.bot.set_my_commands = AsyncMock()
    app.initialize = AsyncMock()
    builder.build.return_value = app

    monkeypatch.setattr(
        core_module.Application,
        "builder",
        MagicMock(return_value=builder),
    )
    monkeypatch.setattr(
        core_module,
        "FeatureRegistry",
        MagicMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(bot, "_set_bot_commands", AsyncMock())
    monkeypatch.setattr(bot, "_register_handlers", MagicMock())
    monkeypatch.setattr(bot, "_add_middleware", MagicMock())

    return bot, builder


class TestRedactProxyUrl:
    """_redact_proxy_url must hide the password and nothing else."""

    def test_password_is_masked(self):
        assert core_module._redact_proxy_url(PROXY_URL) == REDACTED_PROXY_URL

    def test_username_scheme_host_and_port_survive(self):
        redacted = core_module._redact_proxy_url(
            "https://svc-bot:hunter2@10.0.0.9:8080"
        )
        assert redacted.startswith("https://svc-bot:***@")
        assert "10.0.0.9:8080" in redacted
        assert "hunter2" not in redacted

    def test_url_without_credentials_is_untouched(self):
        url = "http://proxy.example.com:3128"
        assert core_module._redact_proxy_url(url) == url

    def test_user_only_token_is_masked(self):
        """Some proxies take a token as the user name."""
        redacted = core_module._redact_proxy_url("http://tok3n@proxy.example.com:3128")
        assert redacted == "http://***@proxy.example.com:3128"

    def test_url_without_scheme_is_masked(self):
        redacted = core_module._redact_proxy_url("alice:s3cret@proxy.example.com:3128")
        assert redacted == "alice:***@proxy.example.com:3128"

    def test_password_containing_at_sign_is_masked(self):
        """rpartition on '@' must split on the userinfo separator."""
        redacted = core_module._redact_proxy_url(
            "http://alice:p@ss@proxy.example.com:3128"
        )
        assert "p@ss" not in redacted
        assert redacted == REDACTED_PROXY_URL

    def test_unparsable_url_is_not_echoed(self):
        """A URL that cannot be parsed must never be logged verbatim."""
        assert core_module._redact_proxy_url("http://[::1") == "<unparsable proxy URL>"


async def test_initialize_configures_proxy_from_environment(
    bot_with_builder, monkeypatch
):
    """HTTPS_PROXY in the environment must reach builder.proxy() unmodified."""
    monkeypatch.setenv("HTTPS_PROXY", PROXY_URL)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    bot, builder = bot_with_builder

    await bot.initialize()

    builder.proxy.assert_called_once_with(PROXY_URL)


@pytest.mark.parametrize("env_var", ["HTTPS_PROXY", "HTTP_PROXY"])
async def test_initialize_does_not_log_proxy_password(
    bot_with_builder, monkeypatch, capsys, env_var
):
    """Regression: the proxy password must not reach the logs."""
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.setenv(env_var, PROXY_URL)
    bot, _builder = bot_with_builder

    with capture_logs() as logs:
        await bot.initialize()

    proxy_events = [e for e in logs if e["event"] == "Proxy configured"]
    assert len(proxy_events) == 1
    assert proxy_events[0]["proxy"] == REDACTED_PROXY_URL
    assert "s3cret" not in repr(logs)

    captured = capsys.readouterr()
    assert "s3cret" not in captured.out
    assert "s3cret" not in captured.err


async def test_initialize_skips_proxy_when_unset(bot_with_builder, monkeypatch):
    """No proxy env vars means builder.proxy() is never called."""
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    bot, builder = bot_with_builder

    await bot.initialize()

    builder.proxy.assert_not_called()


async def test_build_error_does_not_echo_proxy_password(monkeypatch):
    """httpx quotes a rejected proxy URL in its error; the password is masked."""
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "alice:s3cret@proxy.example.com:3128")
    bot = ClaudeCodeBot(create_test_config(), {})

    with pytest.raises(ValueError) as exc_info:
        await bot.initialize()

    assert "s3cret" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True
