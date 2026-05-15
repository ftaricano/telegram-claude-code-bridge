"""Tests for topic-scoped session persistence."""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.database import DatabaseManager
from src.storage.models import TopicSessionModel
from src.storage.repositories import TopicSessionRepository


@pytest.fixture
async def topic_session_repo():
    """Create a topic session repository backed by a temp database."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test.db"
        manager = DatabaseManager(f"sqlite:///{db_path}")
        await manager.initialize()
        yield TopicSessionRepository(manager)
        await manager.close()


def _topic_session(
    *,
    chat_id: int = -100123,
    message_thread_id: int = 42,
    user_id: int = 777,
    session_id: str = "session-a",
    project_path: str = "/workspace/project",
    is_active: bool = True,
) -> TopicSessionModel:
    now = datetime.now(UTC)
    return TopicSessionModel(
        chat_id=chat_id,
        message_thread_id=message_thread_id,
        user_id=user_id,
        session_id=session_id,
        project_path=project_path,
        is_active=is_active,
        created_at=now,
        last_used=now,
    )


async def test_topic_session_crud_round_trip(topic_session_repo):
    """A topic session can be created, read and deleted."""
    model = _topic_session()

    await topic_session_repo.upsert(model)
    stored = await topic_session_repo.get(model.chat_id, model.message_thread_id)

    assert stored == model

    await topic_session_repo.delete(model.chat_id, model.message_thread_id)

    assert await topic_session_repo.get(model.chat_id, model.message_thread_id) is None


async def test_upsert_conflict_updates_existing_row(topic_session_repo):
    """Upsert updates the existing PK row without changing created_at."""
    original = _topic_session(session_id="session-a")
    updated = _topic_session(session_id="session-b", user_id=888)
    updated.created_at = original.created_at + timedelta(days=1)
    updated.last_used = original.last_used + timedelta(minutes=5)

    await topic_session_repo.upsert(original)
    await topic_session_repo.upsert(updated)
    stored = await topic_session_repo.get(original.chat_id, original.message_thread_id)

    assert stored is not None
    assert stored.session_id == "session-b"
    assert stored.user_id == 888
    assert stored.created_at == original.created_at
    assert stored.last_used == updated.last_used


async def test_delete_is_idempotent(topic_session_repo):
    """Deleting a missing topic session is a no-op."""
    await topic_session_repo.delete(-100123, 404)
    await topic_session_repo.delete(-100123, 404)

    assert await topic_session_repo.get(-100123, 404) is None


async def test_set_inactive_missing_row_is_noop(topic_session_repo):
    """set_inactive on a missing row is a no-op."""
    await topic_session_repo.set_inactive(-100123, 404)

    assert await topic_session_repo.get(-100123, 404) is None


async def test_set_inactive_and_reactivate_preserve_session(topic_session_repo):
    """Closing and reopening a topic toggles is_active but keeps session_id."""
    model = _topic_session(session_id="keep-me")
    await topic_session_repo.upsert(model)

    await topic_session_repo.set_inactive(model.chat_id, model.message_thread_id)
    inactive = await topic_session_repo.get(model.chat_id, model.message_thread_id)

    assert inactive is not None
    assert inactive.session_id == "keep-me"
    assert inactive.is_active is False

    await topic_session_repo.reactivate(model.chat_id, model.message_thread_id)
    active = await topic_session_repo.get(model.chat_id, model.message_thread_id)

    assert active is not None
    assert active.session_id == "keep-me"
    assert active.is_active is True


async def test_list_active_returns_only_active_topics(topic_session_repo):
    """list_active excludes closed topics and sorts newest first."""
    first = _topic_session(message_thread_id=1, session_id="old")
    second = _topic_session(message_thread_id=2, session_id="new")
    second.last_used = first.last_used + timedelta(minutes=1)
    closed = _topic_session(message_thread_id=3, session_id="closed", is_active=False)

    await topic_session_repo.upsert(first)
    await topic_session_repo.upsert(second)
    await topic_session_repo.upsert(closed)

    active = await topic_session_repo.list_active()

    assert [row.session_id for row in active] == ["new", "old"]
