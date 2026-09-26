"""Tests for startup hydration of topic context summaries."""

from datetime import UTC, datetime
from types import SimpleNamespace

from src.claude.context_manager import ContextManager
from src.main import hydrate_context_manager_from_topic_sessions


class FakeTopicSessionRepository:
    """Repository returning active topic sessions."""

    async def list_active(self, limit=100):
        return [
            SimpleNamespace(chat_id=-100123, message_thread_id=10),
            SimpleNamespace(chat_id=-100123, message_thread_id=20),
        ][:limit]


class FakeConversationSummaryRepository:
    """Repository returning one latest summary."""

    async def get_latest_for_topic(self, key):
        if key != "-100123:10":
            return None
        return SimpleNamespace(
            summary_text="Resumo persistido",
            created_at=datetime.now(UTC),
            tokens_after=321,
        )


class FakeStorage:
    """Storage subset used by hydration."""

    def __init__(self):
        self.topic_sessions = FakeTopicSessionRepository()
        self.conversation_summaries = FakeConversationSummaryRepository()


async def test_hydrate_context_manager_loads_latest_active_topic_summary():
    """Startup hydration loads latest summary into ContextManager state."""
    manager = ContextManager(
        token_threshold=1000,
        keep_last=2,
        summary_target_tokens=50,
    )

    hydrated = await hydrate_context_manager_from_topic_sessions(
        manager,
        FakeStorage(),
        limit=100,
    )

    state = manager.get_state("-100123:10")
    assert hydrated == 1
    assert state.last_summary_text == "Resumo persistido"
    assert state.tokens_used == 321
    assert state.compaction_count == 1
    assert "-100123:20" not in manager._states
