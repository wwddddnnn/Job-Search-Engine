ALTER TABLE search_runs ADD COLUMN idempotency_scope TEXT;

ALTER TABLE search_runs ADD COLUMN idempotency_key TEXT;

ALTER TABLE search_runs ADD COLUMN execution_token TEXT;

ALTER TABLE search_runs ADD COLUMN execution_lease_expires_at TEXT;

CREATE UNIQUE INDEX idx_search_runs_idempotency
    ON search_runs (idempotency_scope, idempotency_key)
    WHERE idempotency_scope IS NOT NULL AND idempotency_key IS NOT NULL;

CREATE INDEX idx_search_runs_recovery_lease
    ON search_runs (status, execution_lease_expires_at);
