-- JAR-163 F2 durability migration.
-- Runtime migration source lives in src/storage/database.py to match the
-- repository's existing migration runner.

CREATE TABLE IF NOT EXISTS message_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_thread_id INTEGER,
    chunk_idx INTEGER NOT NULL,
    payload_text TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    parse_mode TEXT,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'sent', 'delivered', 'failed')),
    telegram_message_id INTEGER,
    idempotency_key TEXT NOT NULL UNIQUE,
    error TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS telegram_message_ids (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_thread_id INTEGER,
    chunk_idx INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS draft_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    chat_id INTEGER NOT NULL,
    message_thread_id INTEGER,
    draft_id INTEGER NOT NULL,
    payload_text TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'delivered', 'failed')),
    available_at REAL NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS worker_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_depth INTEGER NOT NULL DEFAULT 0,
    dropped_count INTEGER NOT NULL DEFAULT 0,
    worker_lag_ms INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
