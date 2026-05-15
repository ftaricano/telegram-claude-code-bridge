"""Tests for agentic topic-scoped Claude session isolation."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config


class FakeTopicSessionRepository:
    """In-memory topic session repository for orchestrator tests."""

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


def _update(chat_id: int, thread_id: int, text: str = "hello"):
    update = MagicMock()
    update.effective_user.id = 123
    update.effective_chat.id = chat_id
    update.effective_chat.is_forum = True
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
    # PTB sets effective_message = message for regular text updates; mimic it.
    update.effective_message = update.message
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
        content=f"response from {session_id}",
        tools_used=[],
        interrupted=False,
        num_turns=1,
        cost=0.0,
        duration_ms=1,
        is_error=False,
        error_type=None,
    )


async def test_two_topics_never_share_session_id(tmp_path):
    """Two Telegram topics in the same chat get distinct persisted sessions."""
    settings = _settings(tmp_path)
    repo = FakeTopicSessionRepository()
    storage = FakeStorage(repo)
    claude = AsyncMock()
    claude.run_command = AsyncMock(
        side_effect=[_claude_response("session-a"), _claude_response("session-b")]
    )
    orchestrator = MessageOrchestrator(settings, {})

    await orchestrator.agentic_text(
        _update(-100123, 10, "topic a"), _context(settings, storage, claude)
    )
    await orchestrator.agentic_text(
        _update(-100123, 20, "topic b"), _context(settings, storage, claude)
    )

    first_call, second_call = claude.run_command.call_args_list
    assert first_call.kwargs["session_id"] is None
    assert first_call.kwargs["force_new"] is True
    assert second_call.kwargs["session_id"] is None
    assert second_call.kwargs["force_new"] is True
    assert repo.rows[(-100123, 10)].session_id == "session-a"
    assert repo.rows[(-100123, 20)].session_id == "session-b"


async def test_existing_topic_resumes_persisted_session_after_restart(tmp_path):
    """A recreated orchestrator resumes the session persisted for the topic."""
    settings = _settings(tmp_path)
    repo = FakeTopicSessionRepository()
    storage = FakeStorage(repo)
    claude = AsyncMock()
    claude.run_command = AsyncMock(
        side_effect=[
            _claude_response("persisted-session"),
            _claude_response("persisted-session"),
        ]
    )

    await MessageOrchestrator(settings, {}).agentic_text(
        _update(-100123, 10, "first"), _context(settings, storage, claude)
    )
    await MessageOrchestrator(settings, {}).agentic_text(
        _update(-100123, 10, "after restart"), _context(settings, storage, claude)
    )

    second_call = claude.run_command.call_args_list[1]
    assert second_call.kwargs["session_id"] == "persisted-session"
    assert second_call.kwargs["force_new"] is False


async def test_new_command_deletes_only_current_topic(tmp_path):
    """The /new command deletes one topic row without touching another topic."""
    settings = _settings(tmp_path)
    repo = FakeTopicSessionRepository()
    storage = FakeStorage(repo)
    claude = AsyncMock()
    claude.run_command = AsyncMock(
        side_effect=[_claude_response("session-a"), _claude_response("session-b")]
    )
    orchestrator = MessageOrchestrator(settings, {})

    await orchestrator.agentic_text(
        _update(-100123, 10, "topic a"), _context(settings, storage, claude)
    )
    await orchestrator.agentic_text(
        _update(-100123, 20, "topic b"), _context(settings, storage, claude)
    )

    reset_update = _update(-100123, 10, "/new")
    await orchestrator.agentic_new(reset_update, _context(settings, storage, claude))

    assert (-100123, 10) not in repo.rows
    assert repo.rows[(-100123, 20)].session_id == "session-b"


async def test_compact_in_empty_topic_does_not_inherit_user_data_session(tmp_path):
    """compact_context with topic_sessions present must NEVER read stale user_data.

    Regression guard for the case where Storage exposes topic_sessions but the
    current topic has no row yet (fresh topic). The legacy user_data
    `claude_session_id` could carry a session id from another topic or a
    pre-JAR-176 install; compact_context must treat the topic as empty
    (session_id=None) instead.
    """
    settings = _settings(tmp_path)
    repo = FakeTopicSessionRepository()
    storage = FakeStorage(repo)
    storage.conversation_summaries = MagicMock()
    storage.conversation_summaries.create_summary = AsyncMock()

    summary_response = SimpleNamespace(
        session_id="ignored-summary-session",
        content="Resumo do tópico.",
        tools_used=[],
        interrupted=False,
        num_turns=1,
        cost=0.0,
        duration_ms=1,
        is_error=False,
        error_type=None,
    )
    claude = AsyncMock()
    claude.run_command = AsyncMock(return_value=summary_response)

    orchestrator = MessageOrchestrator(settings, {})
    # Seed a previous turn so context_manager.compact has something to summarize.
    key = "-100123:10"
    orchestrator.context_manager.record_turn(
        key,
        user_text="Mensagem anterior",
        assistant_text="Resposta anterior",
        session_id="seeded-session",
    )

    update = _update(-100123, 10, "/compact")
    context = _context(settings, storage, claude)
    # Stale legacy state that MUST NOT bleed into compact_context.
    context.user_data["claude_session_id"] = "stale-leaked-session"

    await orchestrator.compact_context(update, context)

    claude.run_command.assert_awaited_once()
    # context_manager.compact passes session_id=None (force_new=True) when
    # invoking Claude for the summary, regardless of what user_data held.
    summary_call = claude.run_command.await_args
    assert summary_call.kwargs["session_id"] is None
    assert summary_call.kwargs["force_new"] is True
