-- Non-secret LLM configuration only; credentials belong to SecretStore.
CREATE TABLE llm_configs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    api_url TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE llm_selection (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    config_id TEXT REFERENCES llm_configs(id) ON DELETE SET NULL
);
INSERT INTO llm_selection (id, config_id) VALUES (1, NULL);
