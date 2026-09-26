from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.claude.goal_manager import GoalManager
from src.config import create_test_config
from src.storage.facade import Storage


@pytest.fixture
async def storage(tmp_path):
    store = Storage(f"sqlite:///{tmp_path / 'goal-flow.db'}")
    await store.initialize()
    yield store
    await store.close()


class FakeGoalManager(GoalManager):
    def __init__(self, repo, evaluations):
        super().__init__(repo)
        self.evaluations = list(evaluations)

    async def _evaluate_goal(self, goal, transcript_tail):
        return self.evaluations.pop(0)


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
    progress_msg.edit_text = AsyncMock()
    update.message.reply_text.return_value = progress_msg
    return update


def _claude_response(session_id: str, token_count: int = 321):
    return SimpleNamespace(
        session_id=session_id,
        content=f"response from {session_id}",
        tools_used=[],
        interrupted=False,
        num_turns=1,
        token_count=token_count,
        cost=0.0,
        duration_ms=1,
        is_error=False,
        error_type=None,
    )


def _context(settings, storage, claude, goal_manager):
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": settings,
        "claude_integration": claude,
        "storage": storage,
        "rate_limiter": None,
        "audit_logger": None,
    }
    claude.goal_manager = goal_manager
    return context


async def test_goal_set_triggers_first_turn_and_tracks_tokens(storage, tmp_path):
    settings = _settings(tmp_path)
    manager = FakeGoalManager(storage.goals, evaluations=[(False, "Need tests.")])
    claude = AsyncMock()
    claude.goal_manager = manager
    claude.run_command = AsyncMock(return_value=_claude_response("session-a", 456))
    orchestrator = MessageOrchestrator(settings, {})

    await orchestrator.agentic_goal(
        _update(-100123, 10, "/goal Finish the migration"),
        _context(settings, storage, claude, manager),
    )

    active = await storage.goals.get_active(-100123, 10)
    assert active is not None
    assert active.condition == "Finish the migration"
    assert active.token_spend == 456
    assert claude.run_command.await_args.kwargs["prompt"] == "Finish the migration"


async def test_goal_stop_hook_yes_clears_active_goal(storage, tmp_path):
    manager = FakeGoalManager(storage.goals, evaluations=[(True, "Done.")])
    await manager.set_goal(-100123, 10, "Finish")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("assistant: done\n", encoding="utf-8")

    result = await manager.build_stop_hook(-100123, 10)(
        {
            "hook_event_name": "Stop",
            "session_id": "session-a",
            "transcript_path": str(transcript),
            "cwd": str(tmp_path),
            "stop_hook_active": False,
        },
        None,
        {},
    )

    assert result == {}
    assert await storage.goals.get_active(-100123, 10) is None


async def test_goal_stop_hook_no_keeps_goal_active(storage, tmp_path):
    manager = FakeGoalManager(storage.goals, evaluations=[(False, "Keep going.")])
    await manager.set_goal(-100123, 10, "Finish")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("assistant: partial\n", encoding="utf-8")

    result = await manager.build_stop_hook(-100123, 10)(
        {
            "hook_event_name": "Stop",
            "session_id": "session-a",
            "transcript_path": str(transcript),
            "cwd": str(tmp_path),
            "stop_hook_active": False,
        },
        None,
        {},
    )

    assert result == {"decision": "block", "reason": "Keep going."}
    assert await storage.goals.get_active(-100123, 10) is not None
