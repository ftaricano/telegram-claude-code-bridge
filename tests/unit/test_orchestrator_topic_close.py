"""Tests for forum topic close status updates."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config


class FakeTopicSessionRepository:
    """In-memory topic session repository for close-event tests."""

    def __init__(self):
        self.rows = {}

    async def get(self, chat_id, message_thread_id):
        return self.rows.get((chat_id, message_thread_id))

    async def upsert(self, model):
        self.rows[(model.chat_id, model.message_thread_id)] = model

    async def delete(self, chat_id, message_thread_id):
        self.rows.pop((chat_id, message_thread_id), None)

    async def set_inactive(self, chat_id, message_thread_id):
        row = self.rows.get((chat_id, message_thread_id))
        if row:
            row.is_active = False

    async def reactivate(self, chat_id, message_thread_id):
        row = self.rows.get((chat_id, message_thread_id))
        if row:
            row.is_active = True


class FakeStorage:
    """Storage facade subset used by MessageOrchestrator."""

    def __init__(self, repo):
        self.topic_sessions = repo
        self.save_claude_interaction = AsyncMock()


def _settings(tmp_path: Path):
    return create_test_config(approved_directory=str(tmp_path), agentic_mode=True)


def _message_update(chat_id: int, thread_id: int, text: str = "hello"):
    update = MagicMock()
    update.effective_user.id = 123
    update.effective_chat.id = chat_id
    update.effective_chat.is_forum = True
    update.effective_message = update.message
    update.message.chat_id = chat_id
    update.message.chat.id = chat_id
    update.message.chat.type = "supergroup"
    update.message.message_thread_id = thread_id
    update.message.text = text
    update.message.message_id = thread_id
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock()
    progress_msg = AsyncMock()
    progress_msg.delete = AsyncMock()
    update.message.reply_text.return_value = progress_msg
    return update


def _status_update(chat_id: int, thread_id: int, *, closed=False, edited=False):
    update = _message_update(chat_id, thread_id)
    update.message.forum_topic_closed = SimpleNamespace() if closed else None
    update.message.forum_topic_edited = (
        SimpleNamespace(is_closed=True) if edited else None
    )
    update.message.forum_topic_deleted = None
    return update


def _context(settings, storage, claude):
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": settings,
        "claude_integration": claude,
        "storage": storage,
        "rate_limiter": None,
        "audit_logger": None,
    }
    return context


def _claude_response(session_id: str):
    return SimpleNamespace(
        session_id=session_id,
        content="ok",
        tools_used=[],
        interrupted=False,
        num_turns=1,
        cost=0.0,
        duration_ms=1,
        is_error=False,
        error_type=None,
    )


async def test_forum_topic_closed_marks_topic_session_inactive(tmp_path):
    """forum_topic_closed preserves the row and sets is_active=false."""
    settings = _settings(tmp_path)
    repo = FakeTopicSessionRepository()
    storage = FakeStorage(repo)
    claude = AsyncMock()
    claude.run_command = AsyncMock(return_value=_claude_response("session-a"))
    orchestrator = MessageOrchestrator(settings, {})

    await orchestrator.agentic_text(
        _message_update(-100123, 10), _context(settings, storage, claude)
    )
    await orchestrator._handle_forum_topic_status_update(
        _status_update(-100123, 10, closed=True),
        _context(settings, storage, claude),
    )

    row = repo.rows[(-100123, 10)]
    assert row.session_id == "session-a"
    assert row.is_active is False


async def test_next_message_reactivates_closed_topic(tmp_path):
    """A message in a closed topic reactivates the stored session."""
    settings = _settings(tmp_path)
    repo = FakeTopicSessionRepository()
    storage = FakeStorage(repo)
    claude = AsyncMock()
    claude.run_command = AsyncMock(
        side_effect=[_claude_response("session-a"), _claude_response("session-a")]
    )
    orchestrator = MessageOrchestrator(settings, {})

    await orchestrator.agentic_text(
        _message_update(-100123, 10, "first"),
        _context(settings, storage, claude),
    )
    await orchestrator._handle_forum_topic_status_update(
        _status_update(-100123, 10, closed=True),
        _context(settings, storage, claude),
    )
    await orchestrator.agentic_text(
        _message_update(-100123, 10, "after reopen"),
        _context(settings, storage, claude),
    )

    second_call = claude.run_command.call_args_list[1]
    assert second_call.kwargs["session_id"] == "session-a"
    assert second_call.kwargs["force_new"] is False
    assert repo.rows[(-100123, 10)].is_active is True


async def test_forum_topic_edited_closed_marks_inactive(tmp_path):
    """forum_topic_edited with is_closed=true is treated as a close event."""
    settings = _settings(tmp_path)
    repo = FakeTopicSessionRepository()
    storage = FakeStorage(repo)
    claude = AsyncMock()
    claude.run_command = AsyncMock(return_value=_claude_response("session-a"))
    orchestrator = MessageOrchestrator(settings, {})

    await orchestrator.agentic_text(
        _message_update(-100123, 10), _context(settings, storage, claude)
    )
    await orchestrator._handle_forum_topic_status_update(
        _status_update(-100123, 10, edited=True),
        _context(settings, storage, claude),
    )

    assert repo.rows[(-100123, 10)].is_active is False
