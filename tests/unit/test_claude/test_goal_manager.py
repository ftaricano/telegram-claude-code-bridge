import pytest

from src.claude.goal_manager import GoalManager
from src.storage.facade import Storage


@pytest.fixture
async def storage(tmp_path):
    store = Storage(f"sqlite:///{tmp_path / 'goal.db'}")
    await store.initialize()
    yield store
    await store.close()


async def test_set_goal_creates_active_record_and_clear_removes_it(storage):
    manager = GoalManager(storage.goals)

    goal = await manager.set_goal(-100123, 42, "Ship the migration")

    assert goal.condition == "Ship the migration"
    active = await manager.get_status(-100123, 42)
    assert active is not None
    assert active.condition == "Ship the migration"
    assert active.is_active is True

    cleared = await manager.clear(-100123, 42)

    assert cleared is not None
    assert cleared.condition == "Ship the migration"
    assert await manager.get_status(-100123, 42) is None


async def test_set_goal_rejects_invalid_condition(storage):
    manager = GoalManager(storage.goals)

    with pytest.raises(ValueError):
        await manager.set_goal(1, 1, "")

    with pytest.raises(ValueError):
        await manager.set_goal(1, 1, "x" * 4001)


async def test_stop_hook_marks_goal_achieved(storage, tmp_path):
    class YesGoalManager(GoalManager):
        async def _evaluate_goal(self, goal, transcript_tail):
            return True, "The transcript shows completion."

    manager = YesGoalManager(storage.goals)
    await manager.set_goal(-100123, 42, "Finish it")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("assistant: finished\n", encoding="utf-8")

    hook = manager.build_stop_hook(-100123, 42)
    result = await hook(
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
    assert await manager.get_status(-100123, 42) is None
    achieved = await storage.goals.get_latest(-100123, 42)
    assert achieved is not None
    assert achieved.is_active is False
    assert achieved.achieved_at is not None
    assert achieved.last_reason == "The transcript shows completion."
    assert achieved.turn_count == 1


async def test_stop_hook_blocks_when_goal_not_met(storage, tmp_path):
    class NoGoalManager(GoalManager):
        async def _evaluate_goal(self, goal, transcript_tail):
            return False, "Need to run the regression tests."

    manager = NoGoalManager(storage.goals)
    await manager.set_goal(-100123, 42, "Finish it")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("assistant: partial progress\n", encoding="utf-8")

    hook = manager.build_stop_hook(-100123, 42)
    result = await hook(
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

    assert result == {
        "decision": "block",
        "reason": "Need to run the regression tests.",
    }
    active = await manager.get_status(-100123, 42)
    assert active is not None
    assert active.last_reason == "Need to run the regression tests."
    assert active.turn_count == 1


async def test_stop_hook_timeout_fails_safe(storage, tmp_path):
    class TimeoutGoalManager(GoalManager):
        async def _evaluate_goal(self, goal, transcript_tail):
            raise TimeoutError("slow evaluator")

    manager = TimeoutGoalManager(storage.goals)
    await manager.set_goal(-100123, 42, "Finish it")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("assistant: partial progress\n", encoding="utf-8")

    result = await manager.build_stop_hook(-100123, 42)(
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

    assert result["decision"] == "block"
    assert "evaluator error" in result["reason"]


async def test_reset_on_resume_preserves_condition_and_reason(storage):
    manager = GoalManager(storage.goals)
    goal = await manager.set_goal(-100123, 42, "Finish it")
    await storage.goals.increment_turn(-100123, 42, tokens=0, last_reason="not yet")
    await storage.goals.add_token_spend(-100123, 42, 1234)

    await storage.goals.reset_on_resume(-100123, 42)

    active = await manager.get_status(-100123, 42)
    assert active is not None
    assert active.condition == goal.condition
    assert active.last_reason == "not yet"
    assert active.turn_count == 0
    assert active.token_spend == 0
    assert active.started_at >= goal.started_at


def test_parse_evaluator_json_tolerates_markdown_wrapper():
    parsed = GoalManager.parse_evaluator_response(
        '```json\n{"yes": false, "reason": "Need tests."}\n```'
    )

    assert parsed == (False, "Need tests.")
