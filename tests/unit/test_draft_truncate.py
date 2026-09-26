from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.utils.draft_streamer import DraftStreamer
from src.utils.constants import TELEGRAM_MAX_MESSAGE_LENGTH


@pytest.fixture()
def mock_bot():
    bot = MagicMock()
    bot.send_message_draft = AsyncMock(return_value=object())
    return bot


async def test_oversized_draft_splits_instead_of_tail_truncating(mock_bot, monkeypatch):
    events = []
    monkeypatch.setattr(
        "src.bot.utils.draft_streamer.logger.warning",
        lambda event, **kwargs: events.append((event, kwargs)),
    )
    streamer = DraftStreamer(
        bot=mock_bot,
        chat_id=123,
        draft_id=42,
        session_id="session-abc",
    )
    long_text = ("a" * TELEGRAM_MAX_MESSAGE_LENGTH) + ("b" * 100)
    streamer._accumulated_text = long_text

    await streamer.flush()

    sent_chunks = [
        call.kwargs["text"] for call in mock_bot.send_message_draft.call_args_list
    ]
    assert len(sent_chunks) == 2
    assert all(len(chunk) <= TELEGRAM_MAX_MESSAGE_LENGTH for chunk in sent_chunks)
    assert "".join(sent_chunks) == long_text
    assert not any(chunk.startswith("\u2026") for chunk in sent_chunks)
    assert events == [
        (
            "draft_truncated",
            {
                "original_length": len(long_text),
                "truncated_to": len(long_text),
                "chunk_count_if_split": 2,
                "chat_id": 123,
                "session_id": "session-abc",
            },
        )
    ]


async def test_extreme_draft_split_logs_explicit_loss(mock_bot, monkeypatch):
    warnings = []
    errors = []
    monkeypatch.setattr(
        "src.bot.utils.draft_streamer.logger.warning",
        lambda event, **kwargs: warnings.append((event, kwargs)),
    )
    monkeypatch.setattr(
        "src.bot.utils.draft_streamer.logger.error",
        lambda event, **kwargs: errors.append((event, kwargs)),
    )
    streamer = DraftStreamer(
        bot=mock_bot,
        chat_id=123,
        draft_id=42,
        session_id="session-abc",
    )
    long_text = "x" * (TELEGRAM_MAX_MESSAGE_LENGTH * 12)
    streamer._accumulated_text = long_text

    await streamer.flush()

    sent_chunks = [
        call.kwargs["text"] for call in mock_bot.send_message_draft.call_args_list
    ]
    assert len(sent_chunks) == 10
    assert all(len(chunk) <= TELEGRAM_MAX_MESSAGE_LENGTH for chunk in sent_chunks)
    assert len("".join(sent_chunks)) < len(long_text)
    assert "[draft truncated after split safety limit]" in sent_chunks[-1]
    assert warnings[0][0] == "draft_truncated"
    assert errors == [
        (
            "draft_truncation_loss",
            {
                "original_length": len(long_text),
                "truncated_to": sum(len(chunk) for chunk in sent_chunks),
                "chunk_count_if_split": 12,
                "delivered_chunk_count": 10,
                "chat_id": 123,
                "session_id": "session-abc",
            },
        )
    ]
