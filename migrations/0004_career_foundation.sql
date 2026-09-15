CREATE TABLE resume_documents (
    id TEXT PRIMARY KEY,
    file_ref TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('imported', 'text_extracted', 'extraction_failed')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    uploaded_at TEXT NOT NULL,
    parser_version TEXT,
    extracted_text_ref TEXT,
    extraction_error TEXT
);

CREATE INDEX idx_resume_documents_content_hash
    ON resume_documents (content_hash);

CREATE INDEX idx_resume_documents_status_uploaded_at
    ON resume_documents (status, uploaded_at DESC);

CREATE TABLE document_texts (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES resume_documents(id),
    text_ref TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    locator_map_ref TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_document_texts_document_created_at
    ON document_texts (document_id, created_at DESC);

CREATE TABLE llm_extraction_runs (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES resume_documents(id),
    input_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'draft_extracting',
            'draft_ready',
            'draft_failed',
            'under_review',
            'profile_version_published'
        )
    ),
    output_ref TEXT,
    error_summary_json TEXT,
    published_profile_version_id TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX idx_llm_extraction_runs_document_created_at
    ON llm_extraction_runs (document_id, started_at DESC);

CREATE INDEX idx_llm_extraction_runs_status_started_at
    ON llm_extraction_runs (status, started_at DESC);

CREATE TABLE career_profiles (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    tenant_id TEXT,
    display_name TEXT NOT NULL,
    current_version_id TEXT REFERENCES profile_versions(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_career_profiles_owner_id
    ON career_profiles (owner_id);

CREATE TABLE profile_versions (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES career_profiles(id),
    version INTEGER NOT NULL CHECK (version > 0),
    source_summary_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (profile_id, version)
);

CREATE INDEX idx_profile_versions_profile_version
    ON profile_versions (profile_id, version DESC);

CREATE TRIGGER profile_versions_require_next_version
BEFORE INSERT ON profile_versions
FOR EACH ROW
WHEN NEW.version != COALESCE(
    (SELECT MAX(version) FROM profile_versions WHERE profile_id = NEW.profile_id),
    0
) + 1
BEGIN
    SELECT RAISE(ABORT, 'profile_versions must start at 1 and increment by 1');
END;

CREATE TRIGGER profile_versions_are_immutable_on_update
BEFORE UPDATE ON profile_versions
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'profile_versions are immutable');
END;

CREATE TRIGGER profile_versions_are_immutable_on_delete
BEFORE DELETE ON profile_versions
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'profile_versions are immutable');
END;

CREATE TRIGGER career_profiles_current_version_must_belong_to_profile_on_insert
BEFORE INSERT ON career_profiles
FOR EACH ROW
WHEN NEW.current_version_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1
        FROM profile_versions
        WHERE id = NEW.current_version_id AND profile_id = NEW.id
    )
BEGIN
    SELECT RAISE(ABORT, 'career profile current version must belong to the profile');
END;

CREATE TRIGGER career_profiles_current_version_must_belong_to_profile_on_update
BEFORE UPDATE OF current_version_id ON career_profiles
FOR EACH ROW
WHEN NEW.current_version_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1
        FROM profile_versions
        WHERE id = NEW.current_version_id AND profile_id = NEW.id
    )
BEGIN
    SELECT RAISE(ABORT, 'career profile current version must belong to the profile');
END;

CREATE TABLE experiences (
    id TEXT PRIMARY KEY,
    profile_version_id TEXT NOT NULL REFERENCES profile_versions(id),
    organization TEXT NOT NULL,
    role TEXT NOT NULL,
    date_range TEXT,
    summary TEXT,
    verification_status TEXT NOT NULL CHECK (
        verification_status IN ('draft', 'needs_clarification', 'verified', 'rejected')
    ),
    created_at TEXT NOT NULL
);

CREATE INDEX idx_experiences_profile_version
    ON experiences (profile_version_id);

CREATE TABLE experience_achievements (
    id TEXT PRIMARY KEY,
    experience_id TEXT NOT NULL REFERENCES experiences(id),
    action_text TEXT NOT NULL,
    outcome_text TEXT,
    metric_value REAL,
    metric_unit TEXT,
    metric_source_document_id TEXT REFERENCES resume_documents(id),
    metric_user_confirmed_at TEXT,
    verification_status TEXT NOT NULL CHECK (
        verification_status IN ('draft', 'needs_clarification', 'verified', 'rejected')
    ),
    created_at TEXT NOT NULL,
    CHECK (metric_unit IS NULL OR metric_value IS NOT NULL),
    CHECK (
        metric_value IS NULL
        OR metric_source_document_id IS NOT NULL
        OR metric_user_confirmed_at IS NOT NULL
    )
);

CREATE INDEX idx_experience_achievements_experience
    ON experience_achievements (experience_id);

CREATE TABLE skills (
    id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    taxonomy_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (canonical_name, taxonomy_ref)
);

CREATE TABLE experience_skills (
    id TEXT PRIMARY KEY,
    experience_id TEXT NOT NULL REFERENCES experiences(id),
    skill_id TEXT NOT NULL REFERENCES skills(id),
    raw_skill_name TEXT NOT NULL,
    proficiency TEXT,
    verification_status TEXT NOT NULL CHECK (
        verification_status IN ('draft', 'needs_clarification', 'verified', 'rejected')
    ),
    created_at TEXT NOT NULL,
    UNIQUE (experience_id, skill_id)
);

CREATE INDEX idx_experience_skills_skill
    ON experience_skills (skill_id, experience_id);

CREATE TABLE experience_evidence (
    id TEXT PRIMARY KEY,
    experience_id TEXT NOT NULL REFERENCES experiences(id),
    experience_achievement_id TEXT REFERENCES experience_achievements(id),
    source_type TEXT NOT NULL CHECK (source_type IN ('resume_document', 'user_assertion')),
    source_document_id TEXT REFERENCES resume_documents(id),
    source_excerpt TEXT,
    source_locator TEXT,
    confidence REAL,
    verification_status TEXT NOT NULL CHECK (
        verification_status IN ('draft', 'needs_clarification', 'verified', 'rejected')
    ),
    user_verified INTEGER NOT NULL DEFAULT 0 CHECK (user_verified IN (0, 1)),
    verified_at TEXT,
    created_at TEXT NOT NULL,
    CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    CHECK (
        (source_type = 'resume_document'
            AND source_document_id IS NOT NULL
            AND (source_excerpt IS NOT NULL OR source_locator IS NOT NULL))
        OR
        (source_type = 'user_assertion'
            AND source_document_id IS NULL
            AND user_verified = 1
            AND verified_at IS NOT NULL)
    ),
    CHECK (
        (user_verified = 1 AND verification_status = 'verified' AND verified_at IS NOT NULL)
        OR
        (user_verified = 0 AND verification_status IN ('draft', 'needs_clarification', 'rejected')
            AND verified_at IS NULL)
    )
);

CREATE INDEX idx_experience_evidence_verification_status
    ON experience_evidence (verification_status, user_verified, verified_at);

CREATE INDEX idx_experience_evidence_source_document
    ON experience_evidence (source_document_id);
