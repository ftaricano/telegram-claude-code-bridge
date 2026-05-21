"""Tests for migration 007 topic_sessions schema."""

import tempfile
from pathlib import Path

import pytest

from src.storage.database import CURRENT_VERSION, DatabaseManager


@pytest.fixture
async def db_manager():
    """Create test database manager."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test.db"
        manager = DatabaseManager(f"sqlite:///{db_path}")
        await manager.initialize()
        yield manager
        await manager.close()


async def test_migration_007_creates_topic_sessions_schema(db_manager):
    """Migration 7 creates topic_sessions table, PK and indexes."""
    async with db_manager.get_connection() as conn:
        cursor = await conn.execute("PRAGMA table_info(topic_sessions)")
        column_rows = await cursor.fetchall()
        columns = [row[1] for row in column_rows]

        assert columns == [
            "chat_id",
            "message_thread_id",
            "user_id",
            "session_id",
            "project_path",
            "is_active",
            "created_at",
            "last_used",
        ]

        not_null = {row[1]: row[3] for row in column_rows}
        assert all(not_null[column] == 1 for column in columns)

        pk_positions = {row[1]: row[5] for row in column_rows}
        assert pk_positions["chat_id"] == 1
        assert pk_positions["message_thread_id"] == 2

        cursor = await conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='topic_sessions'"
        )
        indexes = {row[0] for row in await cursor.fetchall()}
        assert "idx_topic_sessions_user" in indexes
        assert "idx_topic_sessions_active" in indexes


async def test_migration_007_is_idempotent(tmp_path):
    """Running initialize twice leaves schema at current version without errors."""
    db_path = tmp_path / "test.db"
    manager = DatabaseManager(f"sqlite:///{db_path}")
    await manager.initialize()
    await manager.initialize()

    async with manager.get_connection() as conn:
        cursor = await conn.execute("SELECT MAX(version) FROM schema_version")
        version = await cursor.fetchone()
        assert version[0] == CURRENT_VERSION

        cursor = await conn.execute("""
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'topic_sessions'
        """)
        assert await cursor.fetchone() is not None

    await manager.close()


async def test_memory_database_smoke_sees_topic_sessions_table():
    """The documented :memory: smoke observes migrations through the pool."""
    manager = DatabaseManager(":memory:")
    await manager.initialize()
    await manager.initialize()

    async with manager.get_connection() as conn:
        cursor = await conn.execute("""
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'topic_sessions'
        """)
        assert await cursor.fetchone() is not None

    await manager.close()
