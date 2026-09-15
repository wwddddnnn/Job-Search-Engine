CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_records (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('in_progress', 'completed', 'failed')),
    response_json TEXT,
    error_code TEXT,
    correlation_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    expires_at TEXT,
    UNIQUE (scope, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_idempotency_records_status_expires
    ON idempotency_records (status, expires_at);

CREATE INDEX IF NOT EXISTS idx_idempotency_records_correlation_id
    ON idempotency_records (correlation_id);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    causation_id TEXT,
    actor_id TEXT NOT NULL,
    source TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('succeeded', 'failed', 'denied')),
    before_json TEXT,
    after_json TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_audit_events_target_time
    ON audit_events (target_type, target_id, occurred_at);

CREATE INDEX IF NOT EXISTS idx_audit_events_correlation_time
    ON audit_events (correlation_id, occurred_at);
