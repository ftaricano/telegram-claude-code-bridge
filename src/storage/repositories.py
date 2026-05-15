"""Data access layer using repository pattern.

Features:
- Clean data access API
- Query optimization
- Error handling
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Dict, List, Optional

import structlog

from .database import DatabaseManager
from .models import (
    AuditLogModel,
    ConversationSummaryModel,
    CostTrackingModel,
    DraftQueueModel,
    Goal,
    MessageCheckpointModel,
    MessageModel,
    ProjectThreadModel,
    SessionModel,
    ToolUsageModel,
    TopicSessionModel,
    UserModel,
)

logger = structlog.get_logger()


def _utcnow() -> datetime:
    """Return an aware UTC timestamp for SQLite adapters."""
    return datetime.now(UTC)


def payload_hash(payload_text: str) -> str:
    """Stable hash used in checkpoint and idempotency records."""
    return hashlib.sha256(payload_text.encode("utf-8")).hexdigest()


class DurabilityRepository:
    """Persistence for outbound delivery checkpoints, draft queue and metrics."""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    async def create_message_checkpoint(
        self,
        *,
        session_id: str,
        chat_id: int,
        message_thread_id: Optional[int],
        chunk_idx: int,
        payload_text: str,
        idempotency_key: str,
        parse_mode: Optional[str] = None,
    ) -> MessageCheckpointModel:
        digest = payload_hash(payload_text)
        now = _utcnow()
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO message_checkpoints (
                    session_id, chat_id, message_thread_id, chunk_idx,
                    payload_text, payload_hash, parse_mode, state,
                    idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    session_id,
                    chat_id,
                    message_thread_id,
                    chunk_idx,
                    payload_text,
                    digest,
                    parse_mode,
                    idempotency_key,
                    now,
                    now,
                ),
            )
            await conn.commit()

            cursor = await conn.execute(
                "SELECT * FROM message_checkpoints WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("Failed to create message checkpoint")
            return MessageCheckpointModel.from_row(row)

    async def mark_checkpoint_sent(self, checkpoint_id: int) -> None:
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE message_checkpoints
                SET state = CASE
                        WHEN state = 'delivered' THEN state
                        ELSE 'sent'
                    END,
                    updated_at = ?
                WHERE id = ?
                """,
                (_utcnow(), checkpoint_id),
            )
            await conn.commit()

    async def mark_checkpoint_delivered(
        self,
        checkpoint_id: int,
        *,
        telegram_message_id: int,
        idempotency_key: str,
    ) -> None:
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM message_checkpoints WHERE id = ?",
                (checkpoint_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return
            checkpoint = MessageCheckpointModel.from_row(row)
            await conn.execute(
                """
                UPDATE message_checkpoints
                SET state = 'delivered',
                    telegram_message_id = ?,
                    error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (telegram_message_id, _utcnow(), checkpoint_id),
            )
            await conn.execute(
                """
                INSERT OR IGNORE INTO telegram_message_ids (
                    idempotency_key, session_id, chat_id, message_thread_id,
                    chunk_idx, payload_hash, telegram_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    checkpoint.session_id,
                    checkpoint.chat_id,
                    checkpoint.message_thread_id,
                    checkpoint.chunk_idx,
                    checkpoint.payload_hash,
                    telegram_message_id,
                    _utcnow(),
                ),
            )
            await conn.commit()

    async def mark_checkpoint_failed(self, checkpoint_id: int, error: str) -> None:
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE message_checkpoints
                SET state = 'failed', error = ?, updated_at = ?
                WHERE id = ?
                """,
                (error[:500], _utcnow(), checkpoint_id),
            )
            await conn.commit()

    async def get_telegram_message_id(self, idempotency_key: str) -> Optional[int]:
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT telegram_message_id
                FROM telegram_message_ids
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else None

    async def list_replayable_checkpoints(
        self, limit: int = 100
    ) -> List[MessageCheckpointModel]:
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT *
                FROM message_checkpoints
                WHERE state IN ('pending', 'sent')
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
            return [MessageCheckpointModel.from_row(row) for row in rows]

    async def enqueue_draft(
        self,
        *,
        session_id: Optional[str],
        chat_id: int,
        message_thread_id: Optional[int],
        draft_id: int,
        payload_text: str,
        payload_hash: str,
        available_at: float,
    ) -> DraftQueueModel:
        now = _utcnow()
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO draft_queue (
                    session_id, chat_id, message_thread_id, draft_id,
                    payload_text, payload_hash, state, available_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    session_id,
                    chat_id,
                    message_thread_id,
                    draft_id,
                    payload_text,
                    payload_hash,
                    available_at,
                    now,
                    now,
                ),
            )
            await conn.commit()
            draft_id_row = cursor.lastrowid
            cursor = await conn.execute(
                "SELECT * FROM draft_queue WHERE id = ?", (draft_id_row,)
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("Failed to enqueue draft")
            return DraftQueueModel.from_row(row)

    async def list_due_drafts(
        self,
        *,
        now: float,
        limit: int = 100,
        chat_id: Optional[int] = None,
        draft_id: Optional[int] = None,
    ) -> List[DraftQueueModel]:
        async with self.db.get_connection() as conn:
            filters = ["state = 'queued'", "available_at <= ?"]
            params: List[object] = [now]
            if chat_id is not None:
                filters.append("chat_id = ?")
                params.append(chat_id)
            if draft_id is not None:
                filters.append("draft_id = ?")
                params.append(draft_id)
            params.append(limit)
            cursor = await conn.execute(
                f"""
                SELECT *
                FROM draft_queue
                WHERE {" AND ".join(filters)}
                ORDER BY id ASC
                LIMIT ?
                """,
                params,
            )
            rows = await cursor.fetchall()
            return [DraftQueueModel.from_row(row) for row in rows]

    async def mark_draft_delivered(self, draft_queue_id: int) -> None:
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE draft_queue
                SET state = 'delivered', updated_at = ?
                WHERE id = ?
                """,
                (_utcnow(), draft_queue_id),
            )
            await conn.commit()

    async def draft_queue_depth(self) -> int:
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM draft_queue WHERE state = 'queued'"
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def record_worker_metric(
        self,
        *,
        queue_depth: int,
        dropped_count: int = 0,
        worker_lag_ms: int = 0,
    ) -> None:
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO worker_metrics (
                    queue_depth, dropped_count, worker_lag_ms, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (queue_depth, dropped_count, worker_lag_ms, _utcnow()),
            )
            await conn.commit()

    async def collect_metrics(self) -> Dict[str, int]:
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM draft_queue WHERE state = 'queued'"
            )
            row = await cursor.fetchone()
            queue_depth_current = int(row[0]) if row else 0

            cursor = await conn.execute(
                """
                SELECT
                    COALESCE(MAX(queue_depth), 0),
                    COALESCE(SUM(CASE
                        WHEN created_at >= datetime('now', '-1 hour')
                        THEN dropped_count ELSE 0 END), 0),
                    COALESCE(SUM(CASE
                        WHEN created_at >= datetime('now', '-24 hours')
                        THEN dropped_count ELSE 0 END), 0),
                    COALESCE(MAX(CASE
                        WHEN created_at >= datetime('now', '-1 hour')
                        THEN worker_lag_ms ELSE 0 END), 0)
                FROM worker_metrics
                """
            )
            metric_row = await cursor.fetchone()

            cursor = await conn.execute(
                """
                SELECT state, COUNT(*)
                FROM message_checkpoints
                WHERE state IN ('pending', 'failed')
                GROUP BY state
                """
            )
            checkpoint_counts = {row[0]: int(row[1]) for row in await cursor.fetchall()}

        return {
            "queue_depth_current": queue_depth_current,
            "queue_depth_max": int(metric_row[0]) if metric_row else 0,
            "dropped_count_last_1h": int(metric_row[1]) if metric_row else 0,
            "dropped_count_last_24h": int(metric_row[2]) if metric_row else 0,
            "worker_lag_ms_max": int(metric_row[3]) if metric_row else 0,
            "checkpoints_pending": checkpoint_counts.get("pending", 0),
            "checkpoints_failed": checkpoint_counts.get("failed", 0),
        }


class UserRepository:
    """User data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def get_user(self, user_id: int) -> Optional[UserModel]:
        """Get user by ID."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            return UserModel.from_row(row) if row else None

    async def create_user(self, user: UserModel) -> UserModel:
        """Create new user."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO users
                (user_id, telegram_username, first_seen,
                 last_active, is_allowed)
                VALUES (?, ?, ?, ?, ?)
            """,
                (
                    user.user_id,
                    user.telegram_username,
                    user.first_seen or datetime.now(UTC),
                    user.last_active or datetime.now(UTC),
                    user.is_allowed,
                ),
            )
            await conn.commit()

            logger.info(
                "Created user", user_id=user.user_id, username=user.telegram_username
            )
            return user

    async def update_user(self, user: UserModel):
        """Update user data."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE users
                SET telegram_username = ?, last_active = ?,
                    total_cost = ?, message_count = ?, session_count = ?
                WHERE user_id = ?
            """,
                (
                    user.telegram_username,
                    user.last_active or datetime.now(UTC),
                    user.total_cost,
                    user.message_count,
                    user.session_count,
                    user.user_id,
                ),
            )
            await conn.commit()

    async def get_allowed_users(self) -> List[int]:
        """Get list of allowed user IDs."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT user_id FROM users WHERE is_allowed = TRUE"
            )
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def set_user_allowed(self, user_id: int, allowed: bool):
        """Set user allowed status."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                "UPDATE users SET is_allowed = ? WHERE user_id = ?", (allowed, user_id)
            )
            await conn.commit()

            logger.info("Updated user permissions", user_id=user_id, allowed=allowed)

    async def get_all_users(self) -> List[UserModel]:
        """Get all users."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute("SELECT * FROM users ORDER BY first_seen DESC")
            rows = await cursor.fetchall()
            return [UserModel.from_row(row) for row in rows]


class SessionRepository:
    """Session data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def get_session(self, session_id: str) -> Optional[SessionModel]:
        """Get session by ID."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            )
            row = await cursor.fetchone()
            return SessionModel.from_row(row) if row else None

    async def create_session(self, session: SessionModel) -> SessionModel:
        """Create new session."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO sessions
                (session_id, user_id, project_path, created_at, last_used)
                VALUES (?, ?, ?, ?, ?)
            """,
                (
                    session.session_id,
                    session.user_id,
                    session.project_path,
                    session.created_at,
                    session.last_used,
                ),
            )
            await conn.commit()

            logger.info(
                "Created session",
                session_id=session.session_id,
                user_id=session.user_id,
            )
            return session

    async def update_session(self, session: SessionModel):
        """Update session data."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE sessions
                SET last_used = ?, total_cost = ?, total_turns = ?,
                    message_count = ?, is_active = ?
                WHERE session_id = ?
            """,
                (
                    session.last_used,
                    session.total_cost,
                    session.total_turns,
                    session.message_count,
                    session.is_active,
                    session.session_id,
                ),
            )
            await conn.commit()

    async def get_user_sessions(
        self, user_id: int, active_only: bool = True
    ) -> List[SessionModel]:
        """Get sessions for user."""
        async with self.db.get_connection() as conn:
            query = "SELECT * FROM sessions WHERE user_id = ?"
            params = [user_id]

            if active_only:
                query += " AND is_active = TRUE"

            query += " ORDER BY last_used DESC"

            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [SessionModel.from_row(row) for row in rows]

    async def cleanup_old_sessions(self, days: int = 30) -> int:
        """Mark old sessions as inactive."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE sessions
                SET is_active = FALSE
                WHERE last_used < datetime('now', '-' || ? || ' days')
                  AND is_active = TRUE
            """,
                (days,),
            )
            await conn.commit()

            affected = cursor.rowcount
            logger.info("Cleaned up old sessions", count=affected, days=days)
            return affected

    async def get_sessions_by_project(self, project_path: str) -> List[SessionModel]:
        """Get sessions for a specific project."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM sessions
                WHERE project_path = ? AND is_active = TRUE
                ORDER BY last_used DESC
            """,
                (project_path,),
            )
            rows = await cursor.fetchall()
            return [SessionModel.from_row(row) for row in rows]


class ProjectThreadRepository:
    """Project-thread mapping data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def get_by_chat_thread(
        self, chat_id: int, message_thread_id: int
    ) -> Optional[ProjectThreadModel]:
        """Find active mapping by chat+thread."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM project_threads
                WHERE chat_id = ? AND message_thread_id = ? AND is_active = TRUE
            """,
                (chat_id, message_thread_id),
            )
            row = await cursor.fetchone()
            return ProjectThreadModel.from_row(row) if row else None

    async def get_by_chat_project(
        self, chat_id: int, project_slug: str
    ) -> Optional[ProjectThreadModel]:
        """Find mapping by chat+project slug."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM project_threads
                WHERE chat_id = ? AND project_slug = ?
            """,
                (chat_id, project_slug),
            )
            row = await cursor.fetchone()
            return ProjectThreadModel.from_row(row) if row else None

    async def upsert_mapping(
        self,
        project_slug: str,
        chat_id: int,
        message_thread_id: int,
        topic_name: str,
        is_active: bool = True,
    ) -> ProjectThreadModel:
        """Create or update mapping by unique chat+project key."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO project_threads (
                    project_slug, chat_id, message_thread_id, topic_name, is_active
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, project_slug) DO UPDATE SET
                    message_thread_id = excluded.message_thread_id,
                    topic_name = excluded.topic_name,
                    is_active = excluded.is_active,
                    updated_at = CURRENT_TIMESTAMP
            """,
                (project_slug, chat_id, message_thread_id, topic_name, is_active),
            )
            await conn.commit()

        mapping = await self.get_by_chat_project(
            chat_id=chat_id, project_slug=project_slug
        )
        if not mapping:
            raise RuntimeError("Failed to upsert project thread mapping")
        return mapping

    async def deactivate_missing_projects(
        self, chat_id: int, active_project_slugs: List[str]
    ) -> int:
        """Deactivate mappings for projects no longer enabled/present."""
        async with self.db.get_connection() as conn:
            if active_project_slugs:
                placeholders = ",".join("?" for _ in active_project_slugs)
                query = f"""
                    UPDATE project_threads
                    SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP
                    WHERE chat_id = ?
                      AND project_slug NOT IN ({placeholders})
                      AND is_active = TRUE
                """
                params = [chat_id] + active_project_slugs
                cursor = await conn.execute(query, params)
            else:
                cursor = await conn.execute(
                    """
                    UPDATE project_threads
                    SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP
                    WHERE chat_id = ? AND is_active = TRUE
                """,
                    (chat_id,),
                )
            await conn.commit()
            return cursor.rowcount

    async def list_stale_active_mappings(
        self, chat_id: int, active_project_slugs: List[str]
    ) -> List[ProjectThreadModel]:
        """List active mappings that are no longer enabled/present."""
        async with self.db.get_connection() as conn:
            if active_project_slugs:
                placeholders = ",".join("?" for _ in active_project_slugs)
                query = f"""
                    SELECT * FROM project_threads
                    WHERE chat_id = ?
                      AND is_active = TRUE
                      AND project_slug NOT IN ({placeholders})
                    ORDER BY project_slug ASC
                """
                params = [chat_id] + active_project_slugs
                cursor = await conn.execute(query, params)
            else:
                cursor = await conn.execute(
                    """
                    SELECT * FROM project_threads
                    WHERE chat_id = ? AND is_active = TRUE
                    ORDER BY project_slug ASC
                """,
                    (chat_id,),
                )
            rows = await cursor.fetchall()
            return [ProjectThreadModel.from_row(row) for row in rows]

    async def set_active(self, chat_id: int, project_slug: str, is_active: bool) -> int:
        """Set active flag for a mapping by chat+project."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE project_threads
                SET is_active = ?, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ? AND project_slug = ?
            """,
                (is_active, chat_id, project_slug),
            )
            await conn.commit()
            return cursor.rowcount

    async def list_by_chat(
        self, chat_id: int, active_only: bool = True
    ) -> List[ProjectThreadModel]:
        """List mappings for a chat."""
        async with self.db.get_connection() as conn:
            query = "SELECT * FROM project_threads WHERE chat_id = ?"
            params = [chat_id]
            if active_only:
                query += " AND is_active = TRUE"
            query += " ORDER BY project_slug ASC"
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [ProjectThreadModel.from_row(row) for row in rows]


class ConversationSummaryRepository:
    """Conversation summary data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def create_summary(self, summary: ConversationSummaryModel) -> int:
        """Create a conversation summary and return its ID."""
        async with self.db.get_connection() as conn:
            created_at = summary.created_at or datetime.now(UTC)
            cursor = await conn.execute(
                """
                INSERT INTO conversation_summaries
                (topic_key, session_id, summary_text, messages_included,
                 tokens_before, tokens_after, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    summary.topic_key,
                    summary.session_id,
                    summary.summary_text,
                    summary.messages_included,
                    summary.tokens_before,
                    summary.tokens_after,
                    created_at,
                ),
            )
            await conn.commit()
            return cursor.lastrowid

    async def get_latest_for_topic(
        self, topic_key: str
    ) -> Optional[ConversationSummaryModel]:
        """Get the latest summary for a topic."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM conversation_summaries
                WHERE topic_key = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            """,
                (topic_key,),
            )
            row = await cursor.fetchone()
            return ConversationSummaryModel.from_row(row) if row else None

    async def list_for_topic(
        self, topic_key: str, limit: int = 20
    ) -> List[ConversationSummaryModel]:
        """List summaries for a topic, newest first."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM conversation_summaries
                WHERE topic_key = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            """,
                (topic_key, limit),
            )
            rows = await cursor.fetchall()
            return [ConversationSummaryModel.from_row(row) for row in rows]


class TopicSessionRepository:
    """Topic-scoped Claude session data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def get(
        self, chat_id: int, message_thread_id: int
    ) -> Optional[TopicSessionModel]:
        """Get topic session by chat and thread."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM topic_sessions
                WHERE chat_id = ? AND message_thread_id = ?
            """,
                (chat_id, message_thread_id),
            )
            row = await cursor.fetchone()
            return TopicSessionModel.from_row(row) if row else None

    async def upsert(self, model: TopicSessionModel) -> None:
        """Create or update the topic session."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO topic_sessions (
                    chat_id, message_thread_id, user_id, session_id, project_path,
                    is_active, created_at, last_used
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_thread_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    session_id = excluded.session_id,
                    project_path = excluded.project_path,
                    is_active = excluded.is_active,
                    last_used = excluded.last_used
            """,
                (
                    model.chat_id,
                    model.message_thread_id,
                    model.user_id,
                    model.session_id,
                    model.project_path,
                    model.is_active,
                    model.created_at,
                    model.last_used,
                ),
            )
            await conn.commit()

    async def delete(self, chat_id: int, message_thread_id: int) -> None:
        """Delete a topic session. Missing rows are ignored."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                DELETE FROM topic_sessions
                WHERE chat_id = ? AND message_thread_id = ?
            """,
                (chat_id, message_thread_id),
            )
            await conn.commit()

    async def set_inactive(self, chat_id: int, message_thread_id: int) -> None:
        """Mark a topic session inactive. Missing rows are ignored."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE topic_sessions
                SET is_active = 0, last_used = ?
                WHERE chat_id = ? AND message_thread_id = ?
            """,
                (datetime.now(UTC), chat_id, message_thread_id),
            )
            await conn.commit()

    async def reactivate(self, chat_id: int, message_thread_id: int) -> None:
        """Reactivate a topic session and refresh last_used."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE topic_sessions
                SET is_active = 1, last_used = ?
                WHERE chat_id = ? AND message_thread_id = ?
            """,
                (datetime.now(UTC), chat_id, message_thread_id),
            )
            await conn.commit()

    async def list_active(self, limit: int = 100) -> List[TopicSessionModel]:
        """List active topic sessions, most recently used first."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM topic_sessions
                WHERE is_active = 1
                ORDER BY last_used DESC
                LIMIT ?
            """,
                (limit,),
            )
            rows = await cursor.fetchall()
            return [TopicSessionModel.from_row(row) for row in rows]


class GoalRepository:
    """Topic-scoped autonomous goal data access."""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    async def set_active(self, chat_id: int, thread_id: int, condition: str) -> Goal:
        """Clear any existing active goal and insert a new active goal."""
        now = datetime.now(UTC)
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                DELETE FROM topic_goals
                WHERE chat_id = ? AND message_thread_id = ? AND is_active = 1
                """,
                (chat_id, thread_id),
            )
            await conn.execute(
                """
                INSERT INTO topic_goals (
                    chat_id, message_thread_id, condition, started_at,
                    turn_count, token_spend, last_reason, is_active, achieved_at
                ) VALUES (?, ?, ?, ?, 0, 0, NULL, 1, NULL)
                """,
                (chat_id, thread_id, condition, now),
            )
            await conn.commit()
        goal = await self.get_active(chat_id, thread_id)
        if goal is None:
            raise RuntimeError("Failed to create active goal")
        return goal

    async def get_active(self, chat_id: int, thread_id: int) -> Optional[Goal]:
        """Return the active goal for a topic, if any."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM topic_goals
                WHERE chat_id = ? AND message_thread_id = ? AND is_active = 1
                """,
                (chat_id, thread_id),
            )
            row = await cursor.fetchone()
            return Goal.from_row(row) if row else None

    async def get_latest(self, chat_id: int, thread_id: int) -> Optional[Goal]:
        """Return the active or most recent inactive goal for a topic."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM topic_goals
                WHERE chat_id = ? AND message_thread_id = ?
                ORDER BY is_active DESC, COALESCE(achieved_at, started_at) DESC
                LIMIT 1
                """,
                (chat_id, thread_id),
            )
            row = await cursor.fetchone()
            return Goal.from_row(row) if row else None

    async def increment_turn(
        self, chat_id: int, thread_id: int, tokens: int, last_reason: str
    ) -> None:
        """Increment active goal turn count and optionally token spend."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE topic_goals
                SET turn_count = turn_count + 1,
                    token_spend = token_spend + ?,
                    last_reason = ?
                WHERE chat_id = ? AND message_thread_id = ? AND is_active = 1
                """,
                (max(0, int(tokens)), last_reason, chat_id, thread_id),
            )
            await conn.commit()

    async def add_token_spend(self, chat_id: int, thread_id: int, tokens: int) -> None:
        """Add token spend to the current or most recent goal row."""
        if tokens <= 0:
            return
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT is_active FROM topic_goals
                WHERE chat_id = ? AND message_thread_id = ?
                ORDER BY is_active DESC, COALESCE(achieved_at, started_at) DESC
                LIMIT 1
                """,
                (chat_id, thread_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return
            await conn.execute(
                """
                UPDATE topic_goals
                SET token_spend = token_spend + ?
                WHERE chat_id = ? AND message_thread_id = ? AND is_active = ?
                """,
                (int(tokens), chat_id, thread_id, int(row["is_active"])),
            )
            await conn.commit()

    async def mark_achieved(self, chat_id: int, thread_id: int) -> Optional[Goal]:
        """Mark the active goal achieved and return the archived goal."""
        active = await self.get_active(chat_id, thread_id)
        if active is None:
            return None
        now = datetime.now(UTC)
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                DELETE FROM topic_goals
                WHERE chat_id = ? AND message_thread_id = ? AND is_active = 0
                """,
                (chat_id, thread_id),
            )
            await conn.execute(
                """
                UPDATE topic_goals
                SET is_active = 0, achieved_at = ?
                WHERE chat_id = ? AND message_thread_id = ? AND is_active = 1
                """,
                (now, chat_id, thread_id),
            )
            await conn.commit()
        return await self.get_latest(chat_id, thread_id)

    async def clear_active(self, chat_id: int, thread_id: int) -> Optional[Goal]:
        """Clear the active goal without marking it achieved."""
        active = await self.get_active(chat_id, thread_id)
        if active is None:
            return None
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                DELETE FROM topic_goals
                WHERE chat_id = ? AND message_thread_id = ? AND is_active = 1
                """,
                (chat_id, thread_id),
            )
            await conn.commit()
        return active

    async def reset_on_resume(self, chat_id: int, thread_id: int) -> None:
        """Reset active goal timer and counters when a topic session is resumed."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE topic_goals
                SET started_at = ?, turn_count = 0, token_spend = 0
                WHERE chat_id = ? AND message_thread_id = ? AND is_active = 1
                """,
                (datetime.now(UTC), chat_id, thread_id),
            )
            await conn.commit()


class MessageRepository:
    """Message data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def save_message(self, message: MessageModel) -> int:
        """Save message and return ID."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO messages
                (session_id, user_id, timestamp, prompt,
                 response, cost, duration_ms, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    message.session_id,
                    message.user_id,
                    message.timestamp,
                    message.prompt,
                    message.response,
                    message.cost,
                    message.duration_ms,
                    message.error,
                ),
            )
            await conn.commit()
            return cursor.lastrowid

    async def get_session_messages(
        self, session_id: str, limit: int = 50
    ) -> List[MessageModel]:
        """Get messages for session."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM messages
                WHERE session_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (session_id, limit),
            )
            rows = await cursor.fetchall()
            return [MessageModel.from_row(row) for row in rows]

    async def get_user_messages(
        self, user_id: int, limit: int = 100
    ) -> List[MessageModel]:
        """Get messages for user."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM messages
                WHERE user_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (user_id, limit),
            )
            rows = await cursor.fetchall()
            return [MessageModel.from_row(row) for row in rows]

    async def get_recent_messages(self, hours: int = 24) -> List[MessageModel]:
        """Get recent messages."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM messages
                WHERE timestamp > datetime('now', '-' || ? || ' hours')
                ORDER BY timestamp DESC
            """,
                (hours,),
            )
            rows = await cursor.fetchall()
            return [MessageModel.from_row(row) for row in rows]


class ToolUsageRepository:
    """Tool usage data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def save_tool_usage(self, tool_usage: ToolUsageModel) -> int:
        """Save tool usage and return ID."""
        async with self.db.get_connection() as conn:
            tool_input_json = (
                json.dumps(tool_usage.tool_input) if tool_usage.tool_input else None
            )

            cursor = await conn.execute(
                """
                INSERT INTO tool_usage
                (session_id, message_id, tool_name, tool_input,
                 timestamp, success, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    tool_usage.session_id,
                    tool_usage.message_id,
                    tool_usage.tool_name,
                    tool_input_json,
                    tool_usage.timestamp,
                    tool_usage.success,
                    tool_usage.error_message,
                ),
            )
            await conn.commit()
            return cursor.lastrowid

    async def get_session_tool_usage(self, session_id: str) -> List[ToolUsageModel]:
        """Get tool usage for session."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM tool_usage
                WHERE session_id = ?
                ORDER BY timestamp DESC
            """,
                (session_id,),
            )
            rows = await cursor.fetchall()
            return [ToolUsageModel.from_row(row) for row in rows]

    async def get_user_tool_usage(self, user_id: int) -> List[ToolUsageModel]:
        """Get tool usage for user."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT tu.* FROM tool_usage tu
                JOIN sessions s ON tu.session_id = s.session_id
                WHERE s.user_id = ?
                ORDER BY tu.timestamp DESC
            """,
                (user_id,),
            )
            rows = await cursor.fetchall()
            return [ToolUsageModel.from_row(row) for row in rows]

    async def get_tool_stats(self) -> List[Dict[str, any]]:
        """Get tool usage statistics."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    tool_name,
                    COUNT(*) as usage_count,
                    COUNT(DISTINCT session_id) as sessions_used,
                    SUM(CASE WHEN success = TRUE THEN 1 ELSE 0 END) as success_count,
                    SUM(CASE WHEN success = FALSE THEN 1 ELSE 0 END) as error_count
                FROM tool_usage
                GROUP BY tool_name
                ORDER BY usage_count DESC
            """
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


class AuditLogRepository:
    """Audit log data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def log_event(self, audit_log: AuditLogModel) -> int:
        """Log audit event and return ID."""
        async with self.db.get_connection() as conn:
            event_data_json = (
                json.dumps(audit_log.event_data) if audit_log.event_data else None
            )

            cursor = await conn.execute(
                """
                INSERT INTO audit_log
                (user_id, event_type, event_data, success, timestamp, ip_address)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    audit_log.user_id,
                    audit_log.event_type,
                    event_data_json,
                    audit_log.success,
                    audit_log.timestamp,
                    audit_log.ip_address,
                ),
            )
            await conn.commit()
            return cursor.lastrowid

    async def get_user_audit_log(
        self, user_id: int, limit: int = 100
    ) -> List[AuditLogModel]:
        """Get audit log for user."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM audit_log
                WHERE user_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (user_id, limit),
            )
            rows = await cursor.fetchall()
            return [AuditLogModel.from_row(row) for row in rows]

    async def get_recent_audit_log(self, hours: int = 24) -> List[AuditLogModel]:
        """Get recent audit log entries."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM audit_log
                WHERE timestamp > datetime('now', '-' || ? || ' hours')
                ORDER BY timestamp DESC
            """,
                (hours,),
            )
            rows = await cursor.fetchall()
            return [AuditLogModel.from_row(row) for row in rows]


class CostTrackingRepository:
    """Cost tracking data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def update_daily_cost(self, user_id: int, cost: float, date: str = None):
        """Update daily cost for user."""
        if not date:
            date = datetime.now(UTC).strftime("%Y-%m-%d")

        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO cost_tracking (user_id, date, daily_cost, request_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(user_id, date)
                DO UPDATE SET
                    daily_cost = daily_cost + ?,
                    request_count = request_count + 1
            """,
                (user_id, date, cost, cost),
            )
            await conn.commit()

    async def get_user_daily_costs(
        self, user_id: int, days: int = 30
    ) -> List[CostTrackingModel]:
        """Get user's daily costs."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM cost_tracking
                WHERE user_id = ? AND date >= date('now', '-' || ? || ' days')
                ORDER BY date DESC
            """,
                (user_id, days),
            )
            rows = await cursor.fetchall()
            return [CostTrackingModel.from_row(row) for row in rows]

    async def get_total_costs(self, days: int = 30) -> List[Dict[str, any]]:
        """Get total costs by day."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    date,
                    SUM(daily_cost) as total_cost,
                    SUM(request_count) as total_requests,
                    COUNT(DISTINCT user_id) as active_users
                FROM cost_tracking
                WHERE date >= date('now', '-' || ? || ' days')
                GROUP BY date
                ORDER BY date DESC
            """,
                (days,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


class AnalyticsRepository:
    """Analytics and reporting."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def get_user_stats(self, user_id: int) -> Dict[str, any]:
        """Get user statistics."""
        async with self.db.get_connection() as conn:
            # User summary
            cursor = await conn.execute(
                """
                SELECT
                    COUNT(DISTINCT session_id) as total_sessions,
                    COUNT(*) as total_messages,
                    SUM(cost) as total_cost,
                    AVG(cost) as avg_cost,
                    MAX(timestamp) as last_activity,
                    AVG(duration_ms) as avg_duration
                FROM messages
                WHERE user_id = ?
            """,
                (user_id,),
            )

            summary = dict(await cursor.fetchone())

            # Daily usage (last 30 days)
            cursor = await conn.execute(
                """
                SELECT
                    date(timestamp) as date,
                    COUNT(*) as messages,
                    SUM(cost) as cost,
                    COUNT(DISTINCT session_id) as sessions
                FROM messages
                WHERE user_id = ? AND timestamp >= datetime('now', '-30 days')
                GROUP BY date(timestamp)
                ORDER BY date DESC
            """,
                (user_id,),
            )

            daily_usage = [dict(row) for row in await cursor.fetchall()]

            # Most used tools
            cursor = await conn.execute(
                """
                SELECT
                    tu.tool_name,
                    COUNT(*) as usage_count
                FROM tool_usage tu
                JOIN sessions s ON tu.session_id = s.session_id
                WHERE s.user_id = ?
                GROUP BY tu.tool_name
                ORDER BY usage_count DESC
                LIMIT 10
            """,
                (user_id,),
            )

            top_tools = [dict(row) for row in await cursor.fetchall()]

            return {
                "summary": summary,
                "daily_usage": daily_usage,
                "top_tools": top_tools,
            }

    async def get_system_stats(self) -> Dict[str, any]:
        """Get system-wide statistics."""
        async with self.db.get_connection() as conn:
            # Overall stats
            cursor = await conn.execute(
                """
                SELECT
                    COUNT(DISTINCT user_id) as total_users,
                    COUNT(DISTINCT session_id) as total_sessions,
                    COUNT(*) as total_messages,
                    SUM(cost) as total_cost,
                    AVG(duration_ms) as avg_duration
                FROM messages
            """
            )

            overall = dict(await cursor.fetchone())

            # Active users (last 7 days)
            cursor = await conn.execute(
                """
                SELECT COUNT(DISTINCT user_id) as active_users
                FROM messages
                WHERE timestamp > datetime('now', '-7 days')
            """
            )

            active_users = (await cursor.fetchone())[0]
            overall["active_users_7d"] = active_users

            # Top users by cost
            cursor = await conn.execute(
                """
                SELECT
                    u.user_id,
                    u.telegram_username,
                    SUM(m.cost) as total_cost,
                    COUNT(m.message_id) as total_messages
                FROM messages m
                JOIN users u ON m.user_id = u.user_id
                GROUP BY u.user_id
                ORDER BY total_cost DESC
                LIMIT 10
            """
            )

            top_users = [dict(row) for row in await cursor.fetchall()]

            # Tool usage stats
            cursor = await conn.execute(
                """
                SELECT
                    tool_name,
                    COUNT(*) as usage_count,
                    COUNT(DISTINCT session_id) as sessions_used
                FROM tool_usage
                GROUP BY tool_name
                ORDER BY usage_count DESC
                LIMIT 10
            """
            )

            tool_stats = [dict(row) for row in await cursor.fetchall()]

            # Daily activity (last 30 days)
            cursor = await conn.execute(
                """
                SELECT
                    date(timestamp) as date,
                    COUNT(DISTINCT user_id) as active_users,
                    COUNT(*) as total_messages,
                    SUM(cost) as total_cost
                FROM messages
                WHERE timestamp >= datetime('now', '-30 days')
                GROUP BY date(timestamp)
                ORDER BY date DESC
            """
            )

            daily_activity = [dict(row) for row in await cursor.fetchall()]

            return {
                "overall": overall,
                "top_users": top_users,
                "tool_stats": tool_stats,
                "daily_activity": daily_activity,
            }
