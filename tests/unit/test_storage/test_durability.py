"""Durability storage tests for JAR-163."""

from pathlib import Path

import pytest

from src.storage.database import DatabaseManager
from src.storage.repositories import DurabilityRepository


@pytest.fixture
async def durability_repo(tmp_path: Path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'durability.db'}")
    await manager.initialize()
    try:
        yield DurabilityRepository(manager)
    finally:
        await manager.close()


async def test_message_checkpoint_lifecycle_is_idempotent(durability_repo):
    checkpoint = await durability_repo.create_message_checkpoint(
        session_id="session-a",
        chat_id=123,
        message_thread_id=10,
        chunk_idx=0,
        payload_text="hello",
        idempotency_key="session-a:0",
    )

    assert checkpoint.state == "pending"
    assert checkpoint.payload_hash

    await durability_repo.mark_checkpoint_sent(checkpoint.id)
    await durability_repo.mark_checkpoint_delivered(
        checkpoint.id,
        telegram_message_id=42,
        idempotency_key="session-a:0",
    )
    await durability_repo.mark_checkpoint_delivered(
        checkpoint.id,
        telegram_message_id=42,
        idempotency_key="session-a:0",
    )

    stored_id = await durability_repo.get_telegram_message_id("session-a:0")
    pending = await durability_repo.list_replayable_checkpoints()
    metrics = await durability_repo.collect_metrics()

    assert stored_id == 42
    assert pending == []
    assert metrics["checkpoints_pending"] == 0


async def test_replayable_checkpoints_include_pending_and_sent(durability_repo):
    pending = await durability_repo.create_message_checkpoint(
        session_id="session-a",
        chat_id=123,
        message_thread_id=None,
        chunk_idx=0,
        payload_text="pending",
        idempotency_key="session-a:0",
    )
    sent = await durability_repo.create_message_checkpoint(
        session_id="session-a",
        chat_id=123,
        message_thread_id=None,
        chunk_idx=1,
        payload_text="sent",
        idempotency_key="session-a:1",
    )
    failed = await durability_repo.create_message_checkpoint(
        session_id="session-a",
        chat_id=123,
        message_thread_id=None,
        chunk_idx=2,
        payload_text="failed",
        idempotency_key="session-a:2",
    )

    await durability_repo.mark_checkpoint_sent(sent.id)
    await durability_repo.mark_checkpoint_failed(failed.id, "boom")

    replayable = await durability_repo.list_replayable_checkpoints()

    assert [row.id for row in replayable] == [pending.id, sent.id]


async def test_draft_queue_persists_and_flushes_in_order(durability_repo):
    await durability_repo.enqueue_draft(
        session_id="session-a",
        chat_id=123,
        message_thread_id=None,
        draft_id=9,
        payload_text="first",
        payload_hash="hash-1",
        available_at=10.0,
    )
    await durability_repo.enqueue_draft(
        session_id="session-a",
        chat_id=123,
        message_thread_id=None,
        draft_id=9,
        payload_text="second",
        payload_hash="hash-2",
        available_at=10.0,
    )

    assert await durability_repo.draft_queue_depth() == 2
    assert await durability_repo.list_due_drafts(now=9.9) == []

    due = await durability_repo.list_due_drafts(now=10.0)
    assert [row.payload_text for row in due] == ["first", "second"]

    await durability_repo.mark_draft_delivered(due[0].id)
    assert await durability_repo.draft_queue_depth() == 1


async def test_metrics_aggregate_queue_and_checkpoint_state(durability_repo):
    await durability_repo.create_message_checkpoint(
        session_id="session-a",
        chat_id=123,
        message_thread_id=None,
        chunk_idx=0,
        payload_text="pending",
        idempotency_key="session-a:0",
    )
    failed = await durability_repo.create_message_checkpoint(
        session_id="session-a",
        chat_id=123,
        message_thread_id=None,
        chunk_idx=1,
        payload_text="failed",
        idempotency_key="session-a:1",
    )
    await durability_repo.mark_checkpoint_failed(failed.id, "boom")
    await durability_repo.record_worker_metric(
        queue_depth=7,
        dropped_count=2,
        worker_lag_ms=150,
    )

    metrics = await durability_repo.collect_metrics()

    assert metrics["queue_depth_current"] == 0
    assert metrics["queue_depth_max"] == 7
    assert metrics["dropped_count_last_1h"] == 2
    assert metrics["dropped_count_last_24h"] == 2
    assert metrics["worker_lag_ms_max"] == 150
    assert metrics["checkpoints_pending"] == 1
    assert metrics["checkpoints_failed"] == 1
