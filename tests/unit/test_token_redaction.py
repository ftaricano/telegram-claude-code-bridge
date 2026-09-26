"""Tests for defensive redaction of Telegram bot tokens in logs."""

import io
import logging

from src.main import TelegramTokenRedactionFilter, setup_logging

TOKEN_A = "bot" + "123456789" + ":ABC_def-GHI"
TOKEN_B = "bot" + "987654321" + ":SECRET-token"
TOKEN_C = "bot" + "111222333" + ":ABC_def-GHI"


def test_redacts_telegram_bot_token_in_log_message() -> None:
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=f'HTTP Request: POST https://api.telegram.org/{TOKEN_A}/getMe "HTTP/1.1 200 OK"',
        args=(),
        exc_info=None,
    )

    assert TelegramTokenRedactionFilter().filter(record) is True

    rendered = record.getMessage()
    assert TOKEN_A not in rendered
    assert "bot<REDACTED>" in rendered


def test_redacts_telegram_bot_token_in_format_args() -> None:
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="HTTP Request: %s",
        args=(f"https://api.telegram.org/{TOKEN_B}/deleteWebhook",),
        exc_info=None,
    )

    assert TelegramTokenRedactionFilter().filter(record) is True

    rendered = record.getMessage()
    assert TOKEN_B not in rendered
    assert "bot<REDACTED>" in rendered


def test_setup_logging_redacts_propagated_httpx_records(monkeypatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr("sys.stdout", stream)

    setup_logging(debug=True)
    logging.getLogger("httpx").info(
        'HTTP Request: POST https://api.telegram.org/%s/getMe "HTTP/1.1 200 OK"',
        TOKEN_C,
    )

    rendered = stream.getvalue()
    assert TOKEN_C not in rendered
    assert "bot<REDACTED>" in rendered
