"""Test Claude SDK integration."""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
)
from claude_agent_sdk.types import StreamEvent

from src.claude import sdk_integration
from src.claude.sdk_integration import (
    ClaudeResponse,
    ClaudeSDKManager,
    StreamUpdate,
    _make_can_use_tool_callback,
)
from src.config.settings import Settings
from src.security.validators import SecurityValidator


@pytest.fixture(autouse=True)
def _patch_parse_message():
    """Patch parse_message as identity so mocks can yield typed Message objects."""
    with patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x):
        yield


def _make_assistant_message(text="Test response"):
    """Create an AssistantMessage with proper structure for current SDK version."""
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude-sonnet-4-20250514",
    )


def _make_result_message(**kwargs):
    """Create a ResultMessage with sensible defaults."""
    defaults = {
        "subtype": "success",
        "duration_ms": 1000,
        "duration_api_ms": 800,
        "is_error": False,
        "num_turns": 1,
        "session_id": "test-session",
        "total_cost_usd": 0.05,
        "result": "Success",
    }
    defaults.update(kwargs)
    return ResultMessage(**defaults)


def _mock_client(*messages):
    """Create a mock ClaudeSDKClient that yields the given messages.

    Returns a factory function suitable for patching ClaudeSDKClient.
    Uses connect()/disconnect() pattern (not async context manager).
    """
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()

    async def receive_raw_messages():
        for msg in messages:
            yield msg

    query_mock = AsyncMock()
    query_mock.receive_messages = receive_raw_messages
    client._query = query_mock

    return client


def _mock_client_factory(*messages, capture_options=None):
    """Create a factory that returns a mock client, optionally capturing options."""

    def factory(options):
        if capture_options is not None:
            capture_options.append(options)
        return _mock_client(*messages)

    return factory


class TestClaudeSDKManager:
    """Test Claude SDK manager."""

    @pytest.fixture
    def config(self, tmp_path):
        """Create test config without API key."""
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,  # Short timeout for testing
            enable_mcp=False,
        )

    @pytest.fixture
    def sdk_manager(self, config):
        """Create SDK manager."""
        return ClaudeSDKManager(config)

    async def test_sdk_manager_removes_anthropic_api_key_from_environment(
        self, config, monkeypatch
    ):
        """SDK manager must ignore and remove Anthropic API key environment auth."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "unsupported-anthropic-api-key")

        ClaudeSDKManager(config)

        assert "ANTHROPIC_API_KEY" not in os.environ

    async def test_sdk_manager_initialization_without_api_key(
        self, config, monkeypatch
    ):
        """Test SDK manager initialization uses Claude CLI/OAuth auth."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        ClaudeSDKManager(config)

        assert "ANTHROPIC_API_KEY" not in os.environ

    async def test_execute_command_success(self, sdk_manager):
        """Test successful command execution."""
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(session_id="test-session", total_cost_usd=0.05),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
                session_id="test-session",
            )

        # Verify response
        assert isinstance(response, ClaudeResponse)
        assert response.session_id == "test-session"
        assert response.duration_ms >= 0
        assert not response.is_error
        assert response.cost == 0.05

    async def test_execute_command_uses_result_content(self, sdk_manager):
        """Test that ResultMessage.result is used for content when available."""
        mock_factory = _mock_client_factory(
            _make_assistant_message("Assistant text"),
            _make_result_message(result="Final result from ResultMessage"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert response.content == "Final result from ResultMessage"

    async def test_execute_command_falls_back_to_messages(self, sdk_manager):
        """Test fallback to message extraction when result is None."""
        mock_factory = _mock_client_factory(
            _make_assistant_message("Extracted from messages"),
            _make_result_message(result=None),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert response.content == "Extracted from messages"

    async def test_execute_command_with_streaming(self, sdk_manager):
        """Test command execution with streaming callback."""
        stream_updates = []

        async def stream_callback(update: StreamUpdate):
            stream_updates.append(update)

        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
                stream_callback=stream_callback,
            )

        # Verify streaming was called
        assert len(stream_updates) > 0
        assert any(update.type == "assistant" for update in stream_updates)

    async def test_execute_command_timeout(self, sdk_manager):
        """Test command execution timeout."""
        from src.claude.exceptions import ClaudeTimeoutError

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock()

        async def hanging_receive():
            await asyncio.sleep(5)  # Exceeds 2s timeout
            yield  # Never reached

        query_mock = AsyncMock()
        query_mock.receive_messages = hanging_receive
        client._query = query_mock

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with pytest.raises(ClaudeTimeoutError):
                await sdk_manager.execute_command(
                    prompt="Test prompt",
                    working_directory=Path("/test"),
                )

    async def test_execute_command_passes_mcp_config(self, tmp_path):
        """Test that MCP config is passed to ClaudeAgentOptions when enabled."""
        # Create a valid MCP config file
        mcp_config_file = tmp_path / "mcp_config.json"
        mcp_config_file.write_text(
            '{"mcpServers": {"test-server": {"command": "echo", "args": ["hello"]}}}'
        )

        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            enable_mcp=True,
            mcp_config_path=str(mcp_config_file),
        )

        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        # Verify MCP config was parsed and passed as dict to options
        assert len(captured_options) == 1
        assert captured_options[0].mcp_servers == {
            "test-server": {"command": "echo", "args": ["hello"]}
        }

    async def test_execute_command_no_mcp_when_disabled(self, sdk_manager):
        """Test that MCP config is NOT passed when MCP is disabled."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        # Verify MCP config was NOT set (should be empty default)
        assert len(captured_options) == 1
        assert captured_options[0].mcp_servers == {}

    async def test_execute_command_passes_resume_session(self, sdk_manager):
        """Test that session_id is passed as options.resume for continuation."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(session_id="test-session"),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Continue working",
                working_directory=Path("/test"),
                session_id="existing-session-id",
                continue_session=True,
            )

        assert len(captured_options) == 1
        assert captured_options[0].resume == "existing-session-id"

    async def test_execute_command_passes_max_budget_usd(self, sdk_manager, config):
        """Test that max_budget_usd is passed from config to ClaudeAgentOptions."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert len(captured_options) == 1
        assert captured_options[0].max_budget_usd == config.claude_max_cost_per_request

    async def test_execute_command_no_resume_for_new_session(self, sdk_manager):
        """Test that resume is not set for new sessions."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(session_id="new-session"),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="New prompt",
                working_directory=Path("/test"),
                session_id=None,
                continue_session=False,
            )

        assert len(captured_options) == 1
        assert (
            not hasattr(captured_options[0], "resume") or not captured_options[0].resume
        )

    async def test_retry_on_transient_cli_connection_error(self, sdk_manager):
        """Test that transient CLIConnectionError triggers retry and succeeds."""
        from claude_agent_sdk import CLIConnectionError

        call_count = 0

        async def flaky_receive():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise CLIConnectionError("connection reset")
            # Second attempt succeeds - yield a ResultMessage
            yield

        # Use a config with 2 attempts
        sdk_manager.config.claude_retry_max_attempts = 2

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock()
        query_mock = AsyncMock()
        query_mock.receive_messages = flaky_receive
        client._query = query_mock

        # Should not raise - second attempt succeeds
        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                try:
                    await sdk_manager.execute_command(
                        prompt="Test",
                        working_directory=Path("/test"),
                    )
                except Exception:
                    pass  # Response parsing may fail - what matters is retry happened
        assert call_count == 2

    async def test_no_retry_on_mcp_connection_error(self, sdk_manager):
        """Test that MCP CLIConnectionError is NOT retried."""
        from claude_agent_sdk import CLIConnectionError

        from src.claude.exceptions import ClaudeMCPError

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock(side_effect=CLIConnectionError("mcp server failed"))

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with pytest.raises((ClaudeMCPError, Exception)):
                await sdk_manager.execute_command(
                    prompt="Test",
                    working_directory=Path("/test"),
                )
        # Only called once - no retry for MCP errors
        assert client.query.call_count == 1

    async def test_retry_disabled_when_max_attempts_zero(self, sdk_manager):
        """Test that setting max_attempts=0 effectively disables retries (1 attempt)."""
        sdk_manager.config.claude_retry_max_attempts = 0
        assert max(1, sdk_manager.config.claude_retry_max_attempts) == 1

    def test_is_retryable_error_transient(self, sdk_manager):
        """Test _is_retryable_error returns True for transient connection errors."""
        from claude_agent_sdk import CLIConnectionError

        assert (
            sdk_manager._is_retryable_error(CLIConnectionError("connection reset"))
            is True
        )

    def test_is_retryable_error_mcp(self, sdk_manager):
        """Test _is_retryable_error returns False for MCP errors."""
        from claude_agent_sdk import CLIConnectionError

        assert (
            sdk_manager._is_retryable_error(CLIConnectionError("mcp server failed"))
            is False
        )

    def test_is_retryable_error_timeout(self, sdk_manager):
        """Test _is_retryable_error returns False for timeout errors."""
        assert sdk_manager._is_retryable_error(asyncio.TimeoutError()) is False


class TestClaudeSandboxSettings:
    """Test sandbox and system_prompt settings on ClaudeAgentOptions."""

    @pytest.fixture
    def config(self, tmp_path):
        """Create test config with sandbox enabled."""
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            sandbox_enabled=True,
            sandbox_excluded_commands=["git", "npm"],
        )

    @pytest.fixture
    def sdk_manager(self, config):
        return ClaudeSDKManager(config)

    async def test_sandbox_settings_passed_to_options(self, sdk_manager, tmp_path):
        """Test that sandbox settings are set on ClaudeAgentOptions."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        opts = captured_options[0]
        assert opts.sandbox == {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": True,
            "excludedCommands": ["git", "npm"],
        }

    async def test_system_prompt_set_with_working_directory(
        self, sdk_manager, tmp_path
    ):
        """Test that system_prompt references the working directory."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        opts = captured_options[0]
        assert str(tmp_path) in opts.system_prompt
        assert "relative paths" in opts.system_prompt.lower()

    async def test_disallowed_tools_passed_to_options(self, tmp_path):
        """Test that disallowed_tools from config are passed to ClaudeAgentOptions."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            claude_disallowed_tools=["WebFetch", "WebSearch"],
            disable_tool_validation=False,
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].disallowed_tools == ["WebFetch", "WebSearch"]

    async def test_allowed_tools_passed_to_options(self, tmp_path):
        """Test that allowed_tools from config are passed to ClaudeAgentOptions."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            claude_allowed_tools=["Read", "Write", "Bash"],
            disable_tool_validation=False,
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].allowed_tools == ["Read", "Write", "Bash"]

    async def test_disable_tool_validation_sets_allowed_tools_empty(self, tmp_path):
        """allowed_tools=[] when DISABLE_TOOL_VALIDATION=true.

        The current claude-agent-sdk applies skill defaults with list(options.allowed_tools),
        so passing None crashes before Claude starts.
        """
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            disable_tool_validation=True,
            claude_allowed_tools=["Read", "Write", "Bash"],
            claude_disallowed_tools=["WebFetch"],
        )
        manager = ClaudeSDKManager(config)

        captured_options: list = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].allowed_tools == []
        assert captured_options[0].disallowed_tools == []

    async def test_tool_validation_enabled_passes_configured_tools(self, tmp_path):
        """allowed/disallowed_tools passed when DISABLE_TOOL_VALIDATION=false."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            disable_tool_validation=False,
            claude_allowed_tools=["Read", "Write"],
            claude_disallowed_tools=["WebFetch"],
        )
        manager = ClaudeSDKManager(config)

        captured_options: list = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].allowed_tools == ["Read", "Write"]
        assert captured_options[0].disallowed_tools == ["WebFetch"]

    async def test_empty_cli_path_coerced_to_none(self, tmp_path):
        """Empty CLAUDE_CLI_PATH ('') is coerced to None so SDK auto-discovers the CLI."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            claude_cli_path="",
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].cli_path is None

    async def test_sandbox_disabled_when_config_false(self, tmp_path):
        """Test sandbox is disabled when sandbox_enabled=False."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            sandbox_enabled=False,
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].sandbox["enabled"] is False

    async def test_claude_model_passed_to_options(self, tmp_path):
        """Test that claude_model from config is passed to ClaudeAgentOptions."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            claude_model="claude-sonnet-4-20250514",
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].model == "claude-sonnet-4-20250514"

    async def test_claude_model_none_when_unset(self, tmp_path):
        """Test that model is None when claude_model is not configured."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].model is None


class TestClaudeMCPErrors:
    """Test MCP-specific error handling."""

    @pytest.fixture
    def config(self, tmp_path):
        """Create test config."""
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
        )

    @pytest.fixture
    def sdk_manager(self, config):
        """Create SDK manager."""
        return ClaudeSDKManager(config)

    async def test_mcp_connection_error_raises_mcp_error(self, sdk_manager):
        """Test that MCP connection errors raise ClaudeMCPError."""
        from claude_agent_sdk import CLIConnectionError

        from src.claude.exceptions import ClaudeMCPError

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock(
            side_effect=CLIConnectionError("MCP server failed to start")
        )

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with pytest.raises(ClaudeMCPError) as exc_info:
                await sdk_manager.execute_command(
                    prompt="Test prompt",
                    working_directory=Path("/test"),
                )

        assert "MCP server" in str(exc_info.value)

    async def test_mcp_process_error_raises_mcp_error(self, sdk_manager):
        """Test that MCP process errors raise ClaudeMCPError."""
        from claude_agent_sdk import ProcessError

        from src.claude.exceptions import ClaudeMCPError

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock(
            side_effect=ProcessError("Failed to start MCP server: connection refused")
        )

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with pytest.raises(ClaudeMCPError) as exc_info:
                await sdk_manager.execute_command(
                    prompt="Test prompt",
                    working_directory=Path("/test"),
                )

        assert "MCP" in str(exc_info.value)


class TestCanUseToolCallback:
    """Test the _make_can_use_tool_callback factory and its behavior."""

    @pytest.fixture
    def approved_dir(self, tmp_path):
        return tmp_path

    @pytest.fixture
    def working_dir(self, tmp_path):
        return tmp_path / "project"

    @pytest.fixture
    def security_validator(self):
        """Create a mock SecurityValidator."""
        validator = MagicMock()
        validator.validate_path = MagicMock(return_value=(True, Path("/ok"), None))
        return validator

    @pytest.fixture
    def callback(self, security_validator, working_dir, approved_dir):
        return _make_can_use_tool_callback(
            security_validator=security_validator,
            working_directory=working_dir,
            approved_directory=approved_dir,
        )

    @pytest.fixture
    def context(self):
        return ToolPermissionContext()

    async def test_allows_safe_file_read(self, callback, context, security_validator):
        """File read with a valid path is allowed."""
        result = await callback("Read", {"file_path": "src/main.py"}, context)
        assert isinstance(result, PermissionResultAllow)
        security_validator.validate_path.assert_called_once()

    async def test_denies_invalid_file_path(
        self, callback, context, security_validator
    ):
        """File write with a path that fails validation is denied."""
        security_validator.validate_path.return_value = (
            False,
            None,
            "Path traversal detected",
        )
        result = await callback("Write", {"file_path": "../../etc/passwd"}, context)
        assert isinstance(result, PermissionResultDeny)
        assert "Path traversal" in result.message

    async def test_allows_bash_inside_boundary(
        self, callback, context, working_dir, approved_dir
    ):
        """Bash command targeting inside approved dir is allowed."""
        result = await callback(
            "Bash", {"command": f"mkdir -p {approved_dir}/subdir"}, context
        )
        assert isinstance(result, PermissionResultAllow)

    async def test_denies_bash_outside_boundary(self, callback, context):
        """Bash command targeting outside approved dir is denied."""
        result = await callback("Bash", {"command": "mkdir -p /tmp/evil"}, context)
        assert isinstance(result, PermissionResultDeny)
        assert "boundary violation" in result.message.lower()

    async def test_allows_unknown_tool(self, callback, context):
        """Tools not in file/bash sets are allowed through."""
        result = await callback("Grep", {"pattern": "foo"}, context)
        assert isinstance(result, PermissionResultAllow)

    async def test_allows_bash_read_only_command(self, callback, context):
        """Read-only bash commands pass through even with external paths."""
        result = await callback("Bash", {"command": "cat /etc/hosts"}, context)
        assert isinstance(result, PermissionResultAllow)

    async def test_file_tool_without_path_allowed(self, callback, context):
        """File tool call without a path key is allowed (no path to validate)."""
        result = await callback("Read", {"content": "something"}, context)
        assert isinstance(result, PermissionResultAllow)

    async def test_wired_into_sdk_manager(self, tmp_path):
        """SecurityValidator is wired into options.can_use_tool by execute_command."""
        validator = MagicMock()
        validator.validate_path = MagicMock(return_value=(True, tmp_path, None))

        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
        )
        manager = ClaudeSDKManager(config, security_validator=validator)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("ok"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(prompt="Test", working_directory=tmp_path)

        assert len(captured_options) == 1
        assert captured_options[0].can_use_tool is not None

    async def test_no_callback_without_security_validator(self, tmp_path):
        """Verify can_use_tool is None when no SecurityValidator is provided."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("ok"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(prompt="Test", working_directory=tmp_path)

        assert len(captured_options) == 1
        assert captured_options[0].can_use_tool is None


class TestGuardedToolsNotPreApproved:
    """Guarded tools must reach can_use_tool instead of being pre-approved.

    ``can_use_tool`` is reactive: the SDK invokes it only when the CLI sends a
    permission request, and the CLI resolves allow rules first. A guarded tool
    left in ``allowed_tools`` is pre-approved and its boundary check never runs.
    Adapted from upstream RichardAtCT/claude-code-telegram PR #220 (MIT).
    """

    @staticmethod
    def _config(tmp_path, **overrides):
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            **overrides,
        )

    @staticmethod
    def _validator(tmp_path):
        validator = MagicMock()
        validator.validate_path = MagicMock(return_value=(True, tmp_path, None))
        return validator

    @staticmethod
    async def _capture(manager, working_directory):
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("ok"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )
        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test", working_directory=working_directory
            )
        assert len(captured_options) == 1
        return captured_options[0]

    async def test_guarded_tools_stripped_from_allowed_tools(self, tmp_path):
        """Default config: no guarded tool is pre-approved via allowed_tools."""
        manager = ClaudeSDKManager(
            self._config(tmp_path), security_validator=self._validator(tmp_path)
        )
        options = await self._capture(manager, tmp_path)

        overlap = sdk_integration.GUARDED_TOOLS & set(options.allowed_tools)
        assert overlap == set(), f"guarded tools pre-approved: {overlap}"
        for tool in ("Read", "Write", "Edit", "MultiEdit", "Bash", "NotebookEdit"):
            assert tool not in options.allowed_tools

    async def test_unguarded_tools_still_allowed(self, tmp_path):
        """Tools the callback does not guard keep their allow rule."""
        manager = ClaudeSDKManager(
            self._config(tmp_path), security_validator=self._validator(tmp_path)
        )
        options = await self._capture(manager, tmp_path)

        for tool in ("Glob", "Grep", "LS", "WebSearch", "TodoWrite"):
            assert tool in options.allowed_tools

    async def test_sandbox_auto_allow_bash_disabled(self, tmp_path):
        """autoAllowBashIfSandboxed would bypass the bash boundary check."""
        manager = ClaudeSDKManager(
            self._config(tmp_path), security_validator=self._validator(tmp_path)
        )
        options = await self._capture(manager, tmp_path)

        assert options.sandbox["autoAllowBashIfSandboxed"] is False

    async def test_sandbox_unsandboxed_commands_disabled(self, tmp_path):
        """Bash may not leave the sandbox while the boundary checks are wired."""
        manager = ClaudeSDKManager(
            self._config(tmp_path), security_validator=self._validator(tmp_path)
        )
        options = await self._capture(manager, tmp_path)

        assert options.sandbox["allowUnsandboxedCommands"] is False

    async def test_default_config_excludes_no_command_from_sandbox(self, tmp_path):
        """An excluded command takes its whole command line out of the sandbox."""
        manager = ClaudeSDKManager(
            self._config(tmp_path), security_validator=self._validator(tmp_path)
        )
        options = await self._capture(manager, tmp_path)

        assert options.sandbox["excludedCommands"] == []

    async def test_boundary_checks_pin_default_permission_mode(self, tmp_path):
        """Without --permission-mode the CLI takes permissions.defaultMode from
        the loaded settings, and bypassPermissions there skips can_use_tool."""
        manager = ClaudeSDKManager(
            self._config(tmp_path), security_validator=self._validator(tmp_path)
        )
        options = await self._capture(manager, tmp_path)

        assert options.permission_mode == "default"

    async def test_no_security_validator_leaves_permission_mode_unset(self, tmp_path):
        """Without the callback there is nothing to keep the mode for."""
        manager = ClaudeSDKManager(self._config(tmp_path))
        options = await self._capture(manager, tmp_path)

        assert options.permission_mode is None

    @staticmethod
    async def _run_pre_tool_use_hooks(options, tool_name, tool_input):
        outputs = []
        for matcher in (options.hooks or {}).get("PreToolUse", []):
            for hook in matcher.hooks:
                outputs.append(
                    await hook(
                        {
                            "hook_event_name": "PreToolUse",
                            "tool_name": tool_name,
                            "tool_input": tool_input,
                        },
                        "toolu_test",
                        None,
                    )
                )
        return outputs

    @staticmethod
    def _decisions(outputs):
        return [
            out.get("hookSpecificOutput", {}).get("permissionDecision")
            for out in outputs
        ]

    async def test_boundary_checks_registered_as_pre_tool_use_hook(self, tmp_path):
        """A subagent whose definition sets permissionMode: bypassPermissions
        never asks can_use_tool; a PreToolUse hook still runs on its tools."""
        approved = tmp_path / "approved"
        approved.mkdir()
        manager = ClaudeSDKManager(
            self._config(approved), security_validator=SecurityValidator(approved)
        )
        options = await self._capture(manager, approved)

        outside = str(tmp_path / "outside.txt")
        outputs = await self._run_pre_tool_use_hooks(
            options, "Write", {"file_path": outside, "content": "x"}
        )
        assert "deny" in self._decisions(outputs)
        reasons = [
            out["hookSpecificOutput"]["permissionDecisionReason"]
            for out in outputs
            if out.get("hookSpecificOutput")
        ]
        assert any("outside approved directory" in r for r in reasons)

        outputs = await self._run_pre_tool_use_hooks(
            options, "Bash", {"command": f"touch {outside}"}
        )
        assert "deny" in self._decisions(outputs)

    async def test_boundary_hook_never_allows(self, tmp_path):
        """Passing the checks yields no decision, so the CLI still asks
        can_use_tool where it would have; "allow" would skip that request."""
        manager = ClaudeSDKManager(
            self._config(tmp_path), security_validator=SecurityValidator(tmp_path)
        )
        options = await self._capture(manager, tmp_path)

        for tool_name, tool_input in (
            ("Write", {"file_path": str(tmp_path / "ok.txt"), "content": "x"}),
            ("Bash", {"command": "ls"}),
            ("Glob", {"pattern": "/etc/*"}),
        ):
            outputs = await self._run_pre_tool_use_hooks(options, tool_name, tool_input)
            assert outputs, "boundary hook not registered"
            assert self._decisions(outputs) == [None] * len(outputs)
        assert options.can_use_tool is not None

    async def test_no_security_validator_registers_no_boundary_hook(self, tmp_path):
        """Without a validator there are no checks to run as a hook."""
        manager = ClaudeSDKManager(self._config(tmp_path))
        options = await self._capture(manager, tmp_path)

        assert not (options.hooks or {}).get("PreToolUse")

    async def test_boundary_hook_keeps_other_pre_tool_use_hooks(self, tmp_path):
        """The boundary hook is added next to the AskUserQuestion hook."""
        manager = ClaudeSDKManager(
            self._config(tmp_path), security_validator=SecurityValidator(tmp_path)
        )
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("ok"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )
        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test",
                working_directory=tmp_path,
                ask_user_question_bot=MagicMock(),
                ask_user_question_chat_id=1,
            )
        matchers = [m.matcher for m in captured_options[0].hooks["PreToolUse"]]
        assert "AskUserQuestion" in matchers
        assert len(matchers) == 2

    @staticmethod
    def _framework_files(root):
        """Route files of common web frameworks, named with '...' or '$'."""
        catch_all = root / "app" / "blog" / "[...slug]" / "page.tsx"
        param = root / "app" / "routes" / "posts.$postId.tsx"
        for path in (catch_all, param):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("export default null\n")
        return catch_all, param

    async def test_boundary_hook_reads_framework_file_names(self, tmp_path):
        """Reads inside the directory are checked for the boundary only.

        Claude Code reads these without asking, so the path validator's shell
        patterns ('..', '$X', ...) must not deny them in the hook.
        """
        approved = tmp_path / "approved"
        approved.mkdir()
        catch_all, param = self._framework_files(approved)
        manager = ClaudeSDKManager(
            self._config(
                approved, claude_allowed_tools=["Read", "NotebookRead", "read_file"]
            ),
            security_validator=SecurityValidator(approved),
        )
        options = await self._capture(manager, approved)

        for tool_name, key in (
            ("Read", "file_path"),
            ("NotebookRead", "notebook_path"),
            ("read_file", "path"),
        ):
            for path in (
                str(catch_all),
                str(param),
                str(catch_all.relative_to(approved)),
                str(param.relative_to(approved)),
            ):
                outputs = await self._run_pre_tool_use_hooks(
                    options, tool_name, {key: path}
                )
                assert self._decisions(outputs) == [None] * len(outputs), (
                    tool_name,
                    path,
                )

    async def test_boundary_hook_denies_reads_outside(self, tmp_path, monkeypatch):
        """A read path that resolves outside the directory is still denied."""
        approved = tmp_path / "approved"
        approved.mkdir()
        (tmp_path / "outside").mkdir()
        (tmp_path / "outside" / "x").write_text("secret\n")
        (approved / "link").symlink_to(tmp_path / "outside")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        catch_all, _param = self._framework_files(approved)
        manager = ClaudeSDKManager(
            self._config(approved), security_validator=SecurityValidator(approved)
        )
        options = await self._capture(manager, approved)

        for path in (
            str(approved / ".." / "outside" / "x"),
            "../outside/x",
            str(catch_all.parent / ".." / ".." / ".." / ".." / "outside" / "x"),
            str(approved / "link" / "x"),
            "link/x",
            str(tmp_path / "outside" / "x"),
            "~/x",
        ):
            outputs = await self._run_pre_tool_use_hooks(
                options, "Read", {"file_path": path}
            )
            assert "deny" in self._decisions(outputs), path

    async def test_boundary_hook_read_keeps_tool_name_checks(self, tmp_path):
        """Only the path patterns are skipped for reads, not the tool lists."""
        manager = ClaudeSDKManager(
            self._config(tmp_path, claude_disallowed_tools=["Read"]),
            security_validator=SecurityValidator(tmp_path),
        )
        options = await self._capture(manager, tmp_path)

        outputs = await self._run_pre_tool_use_hooks(
            options, "Read", {"file_path": str(tmp_path / "notes.txt")}
        )
        assert "deny" in self._decisions(outputs)

    async def test_boundary_hook_write_keeps_path_patterns(self, tmp_path):
        """Writes still go through the full path validator."""
        approved = tmp_path / "approved"
        approved.mkdir()
        catch_all, param = self._framework_files(approved)
        manager = ClaudeSDKManager(
            self._config(approved), security_validator=SecurityValidator(approved)
        )
        options = await self._capture(manager, approved)

        for path in (catch_all, param):
            outputs = await self._run_pre_tool_use_hooks(
                options, "Write", {"file_path": str(path), "content": "x"}
            )
            assert "deny" in self._decisions(outputs), path

    async def test_disable_tool_validation_keeps_boundary_checks(self, tmp_path):
        """DISABLE_TOOL_VALIDATION skips name lists, not the path/bash checks."""
        manager = ClaudeSDKManager(
            self._config(tmp_path, disable_tool_validation=True),
            security_validator=self._validator(tmp_path),
        )
        options = await self._capture(manager, tmp_path)

        assert options.allowed_tools == []
        assert options.can_use_tool is not None
        assert options.sandbox["autoAllowBashIfSandboxed"] is False

    async def test_no_security_validator_leaves_allowed_tools_untouched(self, tmp_path):
        """Without a validator there is no callback to route to."""
        config = self._config(tmp_path)
        manager = ClaudeSDKManager(config)
        options = await self._capture(manager, tmp_path)

        assert options.allowed_tools == config.claude_allowed_tools
        assert options.sandbox["autoAllowBashIfSandboxed"] is True

    async def test_custom_allowed_tools_are_filtered_too(self, tmp_path):
        """A user-supplied allowlist is filtered on the same rule."""
        manager = ClaudeSDKManager(
            self._config(tmp_path, claude_allowed_tools=["Read", "Bash", "Grep"]),
            security_validator=self._validator(tmp_path),
        )
        options = await self._capture(manager, tmp_path)

        assert options.allowed_tools == ["Grep"]

    async def test_config_allowed_tools_not_mutated(self, tmp_path):
        """Filtering builds a new list; the Settings value is left intact."""
        config = self._config(tmp_path)
        original = list(config.claude_allowed_tools)
        manager = ClaudeSDKManager(config, security_validator=self._validator(tmp_path))
        await self._capture(manager, tmp_path)

        assert config.claude_allowed_tools == original

    async def test_default_config_denies_write_outside_approved_directory(
        self, tmp_path
    ):
        """End to end with the default config and a real SecurityValidator."""
        approved = tmp_path / "approved"
        approved.mkdir()
        outside = tmp_path / "outside" / "file.txt"
        config = self._config(approved)
        manager = ClaudeSDKManager(
            config, security_validator=SecurityValidator(approved)
        )
        options = await self._capture(manager, approved)

        assert "Write" not in options.allowed_tools
        result = await options.can_use_tool(
            "Write", {"file_path": str(outside)}, ToolPermissionContext()
        )
        assert isinstance(result, PermissionResultDeny)

        inside = await options.can_use_tool(
            "Write", {"file_path": str(approved / "ok.txt")}, ToolPermissionContext()
        )
        assert isinstance(inside, PermissionResultAllow)

    async def test_default_config_denies_tool_outside_allowlist(self, tmp_path):
        """A tool that is not in CLAUDE_ALLOWED_TOOLS is refused by the callback."""
        manager = ClaudeSDKManager(
            self._config(tmp_path), security_validator=self._validator(tmp_path)
        )
        options = await self._capture(manager, tmp_path)

        for tool in ("mcp__example__send_message", "KillShell"):
            result = await options.can_use_tool(tool, {}, ToolPermissionContext())
            assert isinstance(result, PermissionResultDeny), tool
            assert "not allowed" in result.message


class TestCanUseToolAllowlist:
    """Name-based allow/deny enforcement inside the can_use_tool callback."""

    @pytest.fixture
    def security_validator(self, tmp_path):
        validator = MagicMock()
        validator.validate_path = MagicMock(return_value=(True, tmp_path, None))
        return validator

    def _callback(self, security_validator, tmp_path, allowed=None, disallowed=None):
        return _make_can_use_tool_callback(
            security_validator=security_validator,
            working_directory=tmp_path,
            approved_directory=tmp_path,
            allowed_tools=allowed,
            disallowed_tools=disallowed,
        )

    async def test_tool_outside_allowlist_is_denied(self, security_validator, tmp_path):
        callback = self._callback(security_validator, tmp_path, allowed=["Read"])
        result = await callback("WebFetch", {"url": "x"}, ToolPermissionContext())
        assert isinstance(result, PermissionResultDeny)
        assert "WebFetch" in result.message

    async def test_guarded_tool_outside_allowlist_is_denied(
        self, security_validator, tmp_path
    ):
        """Write is denied when only Read is allowed, even for a valid path."""
        callback = self._callback(security_validator, tmp_path, allowed=["Read"])
        result = await callback(
            "Write", {"file_path": str(tmp_path / "a.txt")}, ToolPermissionContext()
        )
        assert isinstance(result, PermissionResultDeny)
        security_validator.validate_path.assert_not_called()

    async def test_disallowed_tool_is_denied(self, security_validator, tmp_path):
        callback = self._callback(
            security_validator, tmp_path, allowed=["Read", "Bash"], disallowed=["Bash"]
        )
        result = await callback("Bash", {"command": "ls"}, ToolPermissionContext())
        assert isinstance(result, PermissionResultDeny)

    async def test_mcp_server_rule_allows_its_tools(self, security_validator, tmp_path):
        """``mcp__<server>`` and ``mcp__<server>__*`` cover every tool of a server."""
        for rule in ("mcp__example", "mcp__example__*"):
            callback = self._callback(security_validator, tmp_path, allowed=[rule])
            ok = await callback("mcp__example__search", {}, ToolPermissionContext())
            other = await callback("mcp__other__search", {}, ToolPermissionContext())
            prefix = await callback(
                "mcp__examplex__search", {}, ToolPermissionContext()
            )
            assert isinstance(ok, PermissionResultAllow), rule
            assert isinstance(other, PermissionResultDeny), rule
            assert isinstance(prefix, PermissionResultDeny), rule

    async def test_no_allowlist_keeps_legacy_behavior(
        self, security_validator, tmp_path
    ):
        """Without an allowlist only the boundary checks apply."""
        callback = self._callback(security_validator, tmp_path)
        result = await callback("WebFetch", {"url": "x"}, ToolPermissionContext())
        assert isinstance(result, PermissionResultAllow)

    async def test_multiedit_and_notebook_paths_are_validated(
        self, security_validator, tmp_path
    ):
        """MultiEdit and notebook tools are path-checked like Write/Edit."""
        security_validator.validate_path.return_value = (False, None, "Outside")
        callback = self._callback(security_validator, tmp_path)
        multi = await callback(
            "MultiEdit", {"file_path": "/etc/hosts"}, ToolPermissionContext()
        )
        notebook = await callback(
            "NotebookEdit", {"notebook_path": "/etc/x.ipynb"}, ToolPermissionContext()
        )
        assert isinstance(multi, PermissionResultDeny)
        assert isinstance(notebook, PermissionResultDeny)
        assert security_validator.validate_path.call_args[0][0] == "/etc/x.ipynb"


def test_default_allowlist_has_no_preapproved_connector_tools(tmp_path):
    """No MCP connector tool is pre-approved by default."""
    config = Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
    )
    assert not [t for t in config.claude_allowed_tools if t.startswith("mcp__")]


class TestSessionIdFallback:
    """Test fallback session ID extraction from StreamEvent messages."""

    @pytest.fixture
    def config(self, tmp_path):
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
        )

    @pytest.fixture
    def sdk_manager(self, config):
        return ClaudeSDKManager(config)

    async def test_session_id_from_stream_event_fallback(self, sdk_manager):
        """Test that session_id is extracted from StreamEvent when ResultMessage has None."""
        stream_event = StreamEvent(
            uuid="evt-1",
            session_id="stream-session-123",
            event={"type": "content_block_delta"},
        )
        mock_factory = _mock_client_factory(
            stream_event,
            _make_assistant_message("Test response"),
            _make_result_message(session_id=None, result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert response.session_id == "stream-session-123"

    async def test_session_id_from_stream_event_empty_string(self, sdk_manager):
        """Test fallback triggers when ResultMessage session_id is empty string."""
        stream_event = StreamEvent(
            uuid="evt-1",
            session_id="stream-session-456",
            event={"type": "content_block_delta"},
        )
        mock_factory = _mock_client_factory(
            stream_event,
            _make_assistant_message("Test response"),
            _make_result_message(session_id="", result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert response.session_id == "stream-session-456"

    async def test_no_fallback_when_result_has_session_id(self, sdk_manager):
        """Test that ResultMessage session_id takes priority over StreamEvent."""
        stream_event = StreamEvent(
            uuid="evt-1",
            session_id="stream-session-999",
            event={"type": "content_block_delta"},
        )
        mock_factory = _mock_client_factory(
            stream_event,
            _make_assistant_message("Test response"),
            _make_result_message(session_id="result-session-abc", result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        # ResultMessage session_id should win
        assert response.session_id == "result-session-abc"

    async def test_fallback_skips_stream_events_without_session_id(self, sdk_manager):
        """Test that StreamEvents without session_id are skipped in fallback."""
        stream_event_no_id = StreamEvent(
            uuid="evt-1",
            session_id=None,
            event={"type": "content_block_start"},
        )
        stream_event_with_id = StreamEvent(
            uuid="evt-2",
            session_id="found-session",
            event={"type": "content_block_delta"},
        )
        mock_factory = _mock_client_factory(
            stream_event_no_id,
            stream_event_with_id,
            _make_assistant_message("Test response"),
            _make_result_message(session_id=None, result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert response.session_id == "found-session"

    async def test_no_session_id_anywhere_falls_back_to_input(self, sdk_manager):
        """Test that input session_id is used when neither ResultMessage nor StreamEvent provide one."""
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(session_id=None, result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
                session_id="input-session-id",
            )

        # Should fall back to the input session_id
        assert response.session_id == "input-session-id"


class TestClaudeMdLoading:
    """Tests for CLAUDE.md loading from working directory."""

    @pytest.fixture
    def config(self, tmp_path):
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="test_bot",
            approved_directory=str(tmp_path),
        )

    @pytest.fixture
    def sdk_manager(self, config):
        return ClaudeSDKManager(config)

    async def test_claude_md_appended_to_system_prompt(self, sdk_manager, tmp_path):
        """CLAUDE.md content is appended to system prompt when present."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# Project Rules\nAlways use type hints.")

        captured: list = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("ok"),
            _make_result_message(),
            capture_options=captured,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(prompt="test", working_directory=tmp_path)

        opts = captured[0]
        assert "# Project Rules" in opts.system_prompt
        assert "Always use type hints." in opts.system_prompt

    async def test_system_prompt_unchanged_without_claude_md(
        self, sdk_manager, tmp_path
    ):
        """System prompt is just the base when no CLAUDE.md exists."""
        captured: list = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("ok"),
            _make_result_message(),
            capture_options=captured,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(prompt="test", working_directory=tmp_path)

        opts = captured[0]
        assert "Use relative paths." in opts.system_prompt
        assert "# Project Rules" not in opts.system_prompt

    async def test_setting_sources_default_to_project_only(self, sdk_manager, tmp_path):
        """User settings are not loaded unless CLAUDE_LOAD_USER_SETTINGS is set."""
        captured: list = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("ok"),
            _make_result_message(),
            capture_options=captured,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(prompt="test", working_directory=tmp_path)

        opts = captured[0]
        assert opts.setting_sources == ["project"]

    async def test_setting_sources_include_user_when_enabled(self, config, tmp_path):
        """CLAUDE_LOAD_USER_SETTINGS=true adds the user settings source."""
        sdk_manager = ClaudeSDKManager(
            config.model_copy(update={"claude_load_user_settings": True})
        )
        captured: list = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("ok"),
            _make_result_message(),
            capture_options=captured,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(prompt="test", working_directory=tmp_path)

        opts = captured[0]
        assert opts.setting_sources == ["project", "user"]


class TestAgentConfigWritesDenied:
    """No tool call may write Claude Code settings, hooks or MCP config.

    With the default config and a real SecurityValidator: an allow rule
    written to these files would pre-approve tools for the next session and
    switch the can_use_tool checks off.
    """

    @pytest.fixture
    def layout(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        approved = tmp_path / "approved"
        project = approved / "project"
        for directory in (home / ".claude" / "plans", project / ".claude"):
            directory.mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text("{}")
        (project / "cfg.json").symlink_to(home / ".claude" / "settings.json")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        return home, approved, project

    async def _options(self, approved, project, **overrides):
        config = TestGuardedToolsNotPreApproved._config(approved, **overrides)
        validator = SecurityValidator(
            approved,
            disable_security_patterns=config.disable_security_patterns,
        )
        manager = ClaudeSDKManager(config, security_validator=validator)
        return await TestGuardedToolsNotPreApproved._capture(manager, project)

    async def _decide(self, options, tool, tool_input):
        return await options.can_use_tool(tool, tool_input, ToolPermissionContext())

    @pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
    async def test_user_settings_direct_path(self, layout, tool):
        home, approved, project = layout
        options = await self._options(approved, project)
        for name in ("settings.json", "settings.local.json"):
            result = await self._decide(
                options, tool, {"file_path": str(home / ".claude" / name)}
            )
            assert isinstance(result, PermissionResultDeny), name

    async def test_project_settings_inside_approved_directory(self, layout):
        _home, approved, project = layout
        options = await self._options(approved, project)
        for path in (
            ".claude/settings.json",
            str(project / ".claude" / "settings.local.json"),
            ".claude/hooks/pre.sh",
            ".claude/skills/x/SKILL.md",
            ".claude/commands/deploy.md",
            ".claude/agents/reviewer.md",
            ".mcp.json",
        ):
            result = await self._decide(options, "Write", {"file_path": path})
            assert isinstance(result, PermissionResultDeny), path
            assert "Claude Code settings" in result.message

    async def test_case_variant_of_project_settings(self, layout):
        """On a case-insensitive filesystem .CLAUDE/SETTINGS.JSON is the same file."""
        _home, approved, project = layout
        options = await self._options(approved, project)
        for tool in ("Write", "Edit"):
            result = await self._decide(
                options, tool, {"file_path": str(project / ".CLAUDE" / "SETTINGS.JSON")}
            )
            assert isinstance(result, PermissionResultDeny), tool

    async def test_unsandboxed_bash_is_denied(self, layout):
        """Bash asking to leave the sandbox is refused, whatever the command."""
        _home, approved, project = layout
        options = await self._options(approved, project)
        for command in ("cp notes.txt ~/.zshenv", "echo x > /tmp/outside.txt", "ls"):
            result = await self._decide(
                options,
                "Bash",
                {"command": command, "dangerouslyDisableSandbox": True},
            )
            assert isinstance(result, PermissionResultDeny), command
            assert "sandbox" in result.message
        sandboxed = await self._decide(options, "Bash", {"command": "ls"})
        assert isinstance(sandboxed, PermissionResultAllow)

    async def test_symlink_to_user_settings(self, layout):
        _home, approved, project = layout
        options = await self._options(approved, project)
        result = await self._decide(options, "Write", {"file_path": "cfg.json"})
        assert isinstance(result, PermissionResultDeny)
        assert "Claude Code settings" in result.message

    async def test_tilde_path_with_security_patterns_disabled(self, layout):
        _home, approved, project = layout
        options = await self._options(approved, project, disable_security_patterns=True)
        for path in ("~/.claude/settings.json", "~/elsewhere.txt"):
            result = await self._decide(options, "Write", {"file_path": path})
            assert isinstance(result, PermissionResultDeny), path

    @pytest.mark.parametrize(
        "command",
        [
            "echo '{}' > ~/.claude/settings.json",
            "cd ~/.claude && echo '{}' > settings.json",
            "cd .claude && echo '{}' > settings.local.json",
            "echo '{}' > cfg.json",
        ],
    )
    async def test_bash_writes(self, layout, command):
        _home, approved, project = layout
        options = await self._options(approved, project)
        result = await self._decide(options, "Bash", {"command": command})
        assert isinstance(result, PermissionResultDeny), command

    async def test_reads_and_scratch_dirs_still_allowed(self, layout):
        home, approved, project = layout
        options = await self._options(approved, project)
        allowed = [
            ("Read", {"file_path": ".claude/settings.json"}),
            ("Bash", {"command": "cat .claude/settings.json"}),
            ("Write", {"file_path": str(home / ".claude" / "plans" / "p.md")}),
            ("Write", {"file_path": "src/main.py"}),
        ]
        for tool, tool_input in allowed:
            result = await self._decide(options, tool, tool_input)
            assert isinstance(result, PermissionResultAllow), (tool, tool_input)


class TestSandboxExcludedCommandChains:
    """Bash lines that mix a sandbox-excluded command with others are refused."""

    async def _options(self, tmp_path, **overrides):
        config = TestGuardedToolsNotPreApproved._config(tmp_path, **overrides)
        manager = ClaudeSDKManager(
            config, security_validator=SecurityValidator(tmp_path)
        )
        return await TestGuardedToolsNotPreApproved._capture(manager, tmp_path)

    async def _decide(self, options, command):
        return await options.can_use_tool(
            "Bash", {"command": command}, ToolPermissionContext()
        )

    async def test_chain_with_excluded_command_is_denied(self, tmp_path):
        options = await self._options(tmp_path, sandbox_excluded_commands=["git"])
        result = await self._decide(options, "git; sh evil.sh")
        assert isinstance(result, PermissionResultDeny)
        assert "SANDBOX_EXCLUDED_COMMANDS" in result.message

    async def test_excluded_command_alone_is_allowed(self, tmp_path):
        options = await self._options(tmp_path, sandbox_excluded_commands=["git"])
        for command in ("git", "git status", "git status && git diff"):
            result = await self._decide(options, command)
            assert isinstance(result, PermissionResultAllow), command

    async def test_default_config_does_not_refuse_chains(self, tmp_path):
        options = await self._options(tmp_path)
        result = await self._decide(options, "git; sh evil.sh")
        assert isinstance(result, PermissionResultAllow)

    async def test_no_refusal_when_sandbox_is_disabled(self, tmp_path):
        """Without the sandbox nothing is excluded from it."""
        options = await self._options(
            tmp_path, sandbox_enabled=False, sandbox_excluded_commands=["git"]
        )
        result = await self._decide(options, "git; sh evil.sh")
        assert isinstance(result, PermissionResultAllow)


class TestHardLinkedFileWrites:
    """File tools may not write a file that has other hard links."""

    async def test_write_through_hard_link_is_denied(self, tmp_path):
        project = tmp_path / "project"
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "settings.json").write_text("{}")
        (project / "phard.json").hardlink_to(project / ".claude" / "settings.json")
        (project / "a.txt").write_text("x")
        (project / "b.txt").hardlink_to(project / "a.txt")
        config = TestGuardedToolsNotPreApproved._config(tmp_path)
        manager = ClaudeSDKManager(
            config, security_validator=SecurityValidator(tmp_path)
        )
        options = await TestGuardedToolsNotPreApproved._capture(manager, project)
        for tool in ("Write", "Edit", "MultiEdit"):
            for path in ("phard.json", "a.txt", str(project / "b.txt")):
                result = await options.can_use_tool(
                    tool, {"file_path": path}, ToolPermissionContext()
                )
                assert isinstance(result, PermissionResultDeny), (tool, path)
        for tool, path in (("Write", "new.txt"), ("Read", "a.txt")):
            result = await options.can_use_tool(
                tool, {"file_path": path}, ToolPermissionContext()
            )
            assert isinstance(result, PermissionResultAllow), (tool, path)
