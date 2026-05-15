"""End-to-end topic session flow with a mocked Claude SDK."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config
from src.storage.facade import Storage


@pytest.fixture
async def storage(tmp_path):
    """Create real storage for the integration flow."""
    store = Storage(f"sqlite:///{tmp_path / 'bot.db'}")
    await store.initialize()
    yield store
    await store.close()


def _settings(tmp_path: Path):
    return create_test_config(approved_directory=str(tmp_path), agentic_mode=True)


def _update(chat_id: int, thread_id: int, text: str):
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


async def test_e2e_topic_flow(storage, tmp_path):
    """Full topic flow: create, resume, isolate another topic, and /new."""
    settings = _settings(tmp_path)
    claude = AsyncMock()
    claude.run_command = AsyncMock(
        side_effect=[
            _claude_response("topic-a-session"),
            _claude_response("topic-a-session"),
            _claude_response("topic-b-session"),
        ]
    )
    orchestrator = MessageOrchestrator(settings, {})

    await orchestrator.agentic_text(
        _update(-100123, 10, "first in A"),
        _context(settings, storage, claude),
    )
    row_a = await storage.topic_sessions.get(-100123, 10)
    assert row_a is not None
    assert row_a.session_id == "topic-a-session"

    await orchestrator.agentic_text(
        _update(-100123, 10, "second in A"),
        _context(settings, storage, claude),
    )
    second_call = claude.run_command.call_args_list[1]
    assert second_call.kwargs["session_id"] == "topic-a-session"
    assert second_call.kwargs["force_new"] is False

    await orchestrator.agentic_text(
        _update(-100123, 20, "first in B"),
        _context(settings, storage, claude),
    )
    row_b = await storage.topic_sessions.get(-100123, 20)
    assert row_b is not None
    assert row_b.session_id == "topic-b-session"
    assert row_b.session_id != row_a.session_id

    await orchestrator.agentic_new(
        _update(-100123, 10, "/new"),
        _context(settings, storage, claude),
    )

    assert await storage.topic_sessions.get(-100123, 10) is None
    remaining_b = await storage.topic_sessions.get(-100123, 20)
    assert remaining_b is not None
    assert remaining_b.session_id == "topic-b-session"
