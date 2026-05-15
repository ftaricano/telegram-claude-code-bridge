"""Telegram delivery durability tests for JAR-163."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bot.durability import TelegramDeliveryManager


@pytest.fixture
def durability_repo():
    repo = SimpleNamespace()
    repo.create_message_checkpoint = AsyncMock()
    repo.mark_checkpoint_sent = AsyncMock()
    repo.mark_checkpoint_delivered = AsyncMock()
    repo.mark_checkpoint_failed = AsyncMock()
    repo.get_telegram_message_id = AsyncMock(return_value=None)
    repo.list_replayable_checkpoints = AsyncMock(return_value=[])
    repo.enqueue_draft = AsyncMock()
    repo.list_due_drafts = AsyncMock(return_value=[])
    repo.mark_draft_delivered = AsyncMock()
    return repo


async def test_send_reply_writes_checkpoint_before_telegram_call(durability_repo):
    calls = []

    async def create_checkpoint(**kwargs):
        calls.append(("checkpoint", kwargs))
        return SimpleNamespace(id=7)

    async def reply_text(*args, **kwargs):
        calls.append(("telegram", kwargs))
        return SimpleNamespace(message_id=42)

    durability_repo.create_message_checkpoint.side_effect = create_checkpoint
    message = SimpleNamespace(reply_text=AsyncMock(side_effect=reply_text))
    manager = TelegramDeliveryManager(durability_repo)

    result = await manager.send_reply_text(
        message,
        session_id="session-a",
        chat_id=123,
        message_thread_id=10,
        chunk_idx=0,
        text="hello",
        parse_mode="HTML",
    )

    assert result.message_id == 42
    assert calls[0][0] == "checkpoint"
    assert calls[0][1]["payload_text"] == "hello"
    assert calls[1][0] == "telegram"
    durability_repo.mark_checkpoint_delivered.assert_awaited_once()


async def test_send_reply_uses_idempotency_key_without_resending(durability_repo):
    durability_repo.get_telegram_message_id.return_value = 42
    message = SimpleNamespace(reply_text=AsyncMock())
    manager = TelegramDeliveryManager(durability_repo)

    result = await manager.send_reply_text(
        message,
        session_id="session-a",
        chat_id=123,
        message_thread_id=None,
        chunk_idx=0,
        text="hello",
    )

    assert result.message_id == 42
    message.reply_text.assert_not_called()
    durability_repo.create_message_checkpoint.assert_not_called()


async def test_replay_pending_checkpoints_dedups_existing_message_id(durability_repo):
    durability_repo.get_telegram_message_id.return_value = 77
    durability_repo.list_replayable_checkpoints.return_value = [
        SimpleNamespace(
            id=1,
            idempotency_key="session-a:0",
            session_id="session-a",
            chat_id=123,
            message_thread_id=None,
            chunk_idx=0,
            payload_text="hello",
            parse_mode=None,
        )
    ]
    bot = SimpleNamespace(send_message=AsyncMock())
    manager = TelegramDeliveryManager(durability_repo)

    count = await manager.replay_pending(bot)

    assert count == 1
    bot.send_message.assert_not_called()
    durability_repo.mark_checkpoint_delivered.assert_awaited_once_with(
        1,
        telegram_message_id=77,
        idempotency_key="session-a:0",
    )


async def test_send_reply_records_zero_when_telegram_result_has_no_message_id(
    durability_repo,
):
    durability_repo.create_message_checkpoint.return_value = SimpleNamespace(id=8)
    message = SimpleNamespace(reply_text=AsyncMock(return_value=object()))
    manager = TelegramDeliveryManager(durability_repo)

    await manager.send_reply_text(
        message,
        session_id="session-a",
        chat_id=123,
        message_thread_id=None,
        chunk_idx=0,
        text="hello",
    )

    durability_repo.mark_checkpoint_delivered.assert_awaited_once()
    assert (
        durability_repo.mark_checkpoint_delivered.await_args.kwargs[
            "telegram_message_id"
        ]
        == 0
    )


async def test_send_reply_marks_checkpoint_failed_on_send_error(durability_repo):
    durability_repo.create_message_checkpoint.return_value = SimpleNamespace(id=9)
    message = SimpleNamespace(reply_text=AsyncMock(side_effect=RuntimeError("boom")))
    manager = TelegramDeliveryManager(durability_repo)

    with pytest.raises(RuntimeError):
        await manager.send_reply_text(
            message,
            session_id="session-a",
            chat_id=123,
            message_thread_id=None,
            chunk_idx=0,
            text="hello",
        )

    durability_repo.mark_checkpoint_failed.assert_awaited_once_with(9, "boom")


async def test_replay_pending_sends_threaded_parse_mode_checkpoint(durability_repo):
    durability_repo.list_replayable_checkpoints.return_value = [
        SimpleNamespace(
            id=1,
            idempotency_key="session-a:0",
            session_id="session-a",
            chat_id=123,
            message_thread_id=10,
            chunk_idx=0,
            payload_text="<b>hello</b>",
            parse_mode="HTML",
        )
    ]
    bot = SimpleNamespace(send_message=AsyncMock(return_value=object()))
    manager = TelegramDeliveryManager(durability_repo)

    count = await manager.replay_pending(bot)

    assert count == 1
    bot.send_message.assert_awaited_once_with(
        chat_id=123,
        text="<b>hello</b>",
        message_thread_id=10,
        parse_mode="HTML",
    )
    durability_repo.mark_checkpoint_delivered.assert_awaited_once()
    assert (
        durability_repo.mark_checkpoint_delivered.await_args.kwargs[
            "telegram_message_id"
        ]
        == 0
    )


async def test_replay_pending_marks_failed_when_resend_fails(durability_repo):
    durability_repo.list_replayable_checkpoints.return_value = [
        SimpleNamespace(
            id=1,
            idempotency_key="session-a:0",
            session_id="session-a",
            chat_id=123,
            message_thread_id=None,
            chunk_idx=0,
            payload_text="hello",
            parse_mode=None,
        )
    ]
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("boom")))
    manager = TelegramDeliveryManager(durability_repo)

    count = await manager.replay_pending(bot)

    assert count == 0
    durability_repo.mark_checkpoint_failed.assert_awaited_once_with(1, "boom")
