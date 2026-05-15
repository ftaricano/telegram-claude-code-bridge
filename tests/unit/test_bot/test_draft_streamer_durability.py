"""Draft streamer durability tests for JAR-163."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.bot.utils import draft_streamer as module
from src.bot.utils.draft_streamer import DraftStreamer


async def test_draft_append_during_cooldown_is_persisted(monkeypatch):
    repo = SimpleNamespace(enqueue_draft=AsyncMock())
    bot = MagicMock()
    bot.send_message_draft = AsyncMock()
    streamer = DraftStreamer(
        bot=bot,
        chat_id=123,
        draft_id=50,
        session_id="session-a",
        durability=repo,
    )
    streamer._enabled = False
    streamer._disabled_until = 100.0
    monkeypatch.setattr(module.time, "time", lambda: 90.0)

    await streamer.append_text("queued")

    bot.send_message_draft.assert_not_called()
    repo.enqueue_draft.assert_awaited_once()
    assert repo.enqueue_draft.await_args.kwargs["payload_text"] == "queued"
    assert repo.enqueue_draft.await_args.kwargs["available_at"] == 100.0


async def test_draft_flush_replays_due_queue_before_current_text(monkeypatch):
    repo = SimpleNamespace(
        enqueue_draft=AsyncMock(),
        list_due_drafts=AsyncMock(
            return_value=[
                SimpleNamespace(
                    id=1,
                    chat_id=123,
                    message_thread_id=None,
                    draft_id=50,
                    payload_text="queued",
                )
            ]
        ),
        mark_draft_delivered=AsyncMock(),
    )
    bot = MagicMock()
    bot.send_message_draft = AsyncMock(return_value=SimpleNamespace(message_id=42))
    streamer = DraftStreamer(
        bot=bot,
        chat_id=123,
        draft_id=50,
        session_id="session-a",
        durability=repo,
    )
    monkeypatch.setattr(module.time, "time", lambda: 101.0)

    streamer._accumulated_text = "current"
    await streamer.flush()

    sent_texts = [
        call.kwargs["text"] for call in bot.send_message_draft.await_args_list
    ]
    assert sent_texts[:2] == ["queued", "current"]
    repo.mark_draft_delivered.assert_awaited_once_with(1)
