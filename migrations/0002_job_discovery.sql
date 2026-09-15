CREATE TABLE search_configs (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL CHECK (version > 0),
    name TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE search_runs (
    id TEXT PRIMARY KEY,
    search_config_id TEXT NOT NULL REFERENCES search_configs(id),
    search_config_version INTEGER NOT NULL CHECK (search_config_version > 0),
    query_snapshot_json TEXT NOT NULL,
    trigger_type TEXT NOT NULL CHECK (trigger_type IN ('manual', 'scheduled')),
    provider TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'partially_succeeded', 'failed', 'cancelled')),
    started_at TEXT,
    completed_at TEXT,
    request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
    raw_result_count INTEGER NOT NULL DEFAULT 0 CHECK (raw_result_count >= 0),
    result_count INTEGER NOT NULL DEFAULT 0 CHECK (result_count >= 0),
    error_summary_json TEXT,
    correlation_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_search_runs_status_created_at
    ON search_runs (status, created_at DESC);

CREATE INDEX idx_search_runs_search_config_id
    ON search_runs (search_config_id, created_at DESC);

CREATE TABLE provider_requests (
    id TEXT PRIMARY KEY,
    search_run_id TEXT NOT NULL REFERENCES search_runs(id),
    request_sequence INTEGER NOT NULL CHECK (request_sequence > 0),
    provider TEXT NOT NULL,
    provider_query_json TEXT NOT NULL,
    cursor_value TEXT,
    requested_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    http_status INTEGER,
    result_count INTEGER NOT NULL DEFAULT 0 CHECK (result_count >= 0),
    next_cursor TEXT,
    error_json TEXT,
    UNIQUE (search_run_id, request_sequence)
);

CREATE INDEX idx_provider_requests_search_run_id
    ON provider_requests (search_run_id, request_sequence);

CREATE TABLE canonical_jobs (
    id TEXT PRIMARY KEY,
    company_name TEXT,
    company_domain TEXT,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    location_text TEXT,
    country_code TEXT,
    canonical_url TEXT,
    current_snapshot_id TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_canonical_jobs_title_location
    ON canonical_jobs (normalized_title, location_text);

CREATE INDEX idx_canonical_jobs_company_name
    ON canonical_jobs (company_name);

CREATE TABLE job_sources (
    id TEXT PRIMARY KEY,
    canonical_job_id TEXT NOT NULL REFERENCES canonical_jobs(id),
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    source_url TEXT,
    canonical_url TEXT,
    provider_sources_json TEXT NOT NULL DEFAULT '[]',
    provider_posted_at_raw TEXT,
    provider_posted_at TEXT,
    provider_last_seen_at_raw TEXT,
    provider_last_seen_at TEXT,
    provider_verified_at_raw TEXT,
    provider_verified_at TEXT,
    content_hash TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    latest_raw_job_payload_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (provider, external_id)
);

CREATE INDEX idx_job_sources_canonical_job_id
    ON job_sources (canonical_job_id);

CREATE INDEX idx_job_sources_canonical_url
    ON job_sources (canonical_url);

CREATE TABLE raw_provider_responses (
    id TEXT PRIMARY KEY,
    provider_request_id TEXT NOT NULL UNIQUE REFERENCES provider_requests(id),
    search_run_id TEXT NOT NULL REFERENCES search_runs(id),
    provider TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    response_metadata_json TEXT NOT NULL DEFAULT '{}',
    received_at TEXT NOT NULL
);

CREATE INDEX idx_raw_provider_responses_search_run_id
    ON raw_provider_responses (search_run_id, received_at);

CREATE TABLE raw_job_payloads (
    id TEXT PRIMARY KEY,
    raw_provider_response_id TEXT NOT NULL REFERENCES raw_provider_responses(id),
    provider TEXT NOT NULL,
    external_id TEXT,
    content_hash TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    normalization_status TEXT NOT NULL CHECK (normalization_status IN ('pending', 'succeeded', 'failed')),
    normalization_error_json TEXT,
    job_source_id TEXT REFERENCES job_sources(id),
    received_at TEXT NOT NULL
);

CREATE INDEX idx_raw_job_payloads_response_id
    ON raw_job_payloads (raw_provider_response_id);

CREATE INDEX idx_raw_job_payloads_provider_external_id
    ON raw_job_payloads (provider, external_id);

CREATE TABLE job_snapshots (
    id TEXT PRIMARY KEY,
    canonical_job_id TEXT NOT NULL REFERENCES canonical_jobs(id),
    job_source_id TEXT NOT NULL REFERENCES job_sources(id),
    content_hash TEXT NOT NULL,
    normalizer_version TEXT NOT NULL,
    normalized_job_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (job_source_id, content_hash)
);

CREATE INDEX idx_job_snapshots_canonical_job_id_created_at
    ON job_snapshots (canonical_job_id, created_at DESC);
