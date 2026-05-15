"""JAR-163 idempotency acceptance tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.bot.durability import TelegramDeliveryManager
from src.storage.database import DatabaseManager
from src.storage.repositories import DurabilityRepository


async def test_same_chunk_retry_returns_recorded_message_id_without_duplicate_send(
    tmp_path,
):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'idempotency.db'}")
    await manager.initialize()
    try:
        repo = DurabilityRepository(manager)
        delivery = TelegramDeliveryManager(repo)
        message = SimpleNamespace(
            reply_text=AsyncMock(return_value=SimpleNamespace(message_id=42))
        )

        first = await delivery.send_reply_text(
            message,
            session_id="topic-a-session",
            chat_id=-100,
            message_thread_id=10,
            chunk_idx=0,
            text="same payload",
        )
        second = await delivery.send_reply_text(
            message,
            session_id="topic-a-session",
            chat_id=-100,
            message_thread_id=10,
            chunk_idx=0,
            text="same payload",
        )

        assert first.message_id == 42
        assert second.message_id == 42
        assert message.reply_text.await_count == 1
    finally:
        await manager.close()


async def test_concurrent_topics_use_distinct_idempotency_keys(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'topics.db'}")
    await manager.initialize()
    try:
        repo = DurabilityRepository(manager)
        delivery = TelegramDeliveryManager(repo)
        message = SimpleNamespace(
            reply_text=AsyncMock(
                side_effect=[
                    SimpleNamespace(message_id=101),
                    SimpleNamespace(message_id=202),
                ]
            )
        )

        first = await delivery.send_reply_text(
            message,
            session_id="topic-a-session",
            chat_id=-100,
            message_thread_id=10,
            chunk_idx=0,
            text="same payload",
        )
        second = await delivery.send_reply_text(
            message,
            session_id="topic-b-session",
            chat_id=-100,
            message_thread_id=20,
            chunk_idx=0,
            text="same payload",
        )

        assert (first.message_id, second.message_id) == (101, 202)
        assert message.reply_text.await_count == 2
    finally:
        await manager.close()
