"""JAR-163 crash/restart recovery acceptance tests.

This models the durable effect of a SIGKILL between write-ahead checkpoint and
Telegram delivery: a fresh process sees the pending row and replays it.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.bot.durability import TelegramDeliveryManager, build_idempotency_key
from src.storage.database import DatabaseManager
from src.storage.repositories import DurabilityRepository


async def test_sigkill_after_checkpoint_recovers_pending_message_on_restart(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'chaos.db'}"
    first_process = DatabaseManager(db_url)
    await first_process.initialize()
    idempotency_key = build_idempotency_key(
        session_id="session-a",
        chat_id=123,
        message_thread_id=None,
        chunk_idx=0,
        payload_text="lost during crash",
    )
    try:
        first_repo = DurabilityRepository(first_process)
        await first_repo.create_message_checkpoint(
            session_id="session-a",
            chat_id=123,
            message_thread_id=None,
            chunk_idx=0,
            payload_text="lost during crash",
            idempotency_key=idempotency_key,
        )
    finally:
        await first_process.close()

    restarted_process = DatabaseManager(db_url)
    await restarted_process.initialize()
    try:
        repo_after_restart = DurabilityRepository(restarted_process)
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=9001))
        )
        replayed = await TelegramDeliveryManager(repo_after_restart).replay_pending(bot)

        assert replayed == 1
        bot.send_message.assert_awaited_once_with(
            chat_id=123,
            text="lost during crash",
        )
        assert await repo_after_restart.list_replayable_checkpoints() == []
        assert await repo_after_restart.get_telegram_message_id(idempotency_key) == 9001
    finally:
        await restarted_process.close()
