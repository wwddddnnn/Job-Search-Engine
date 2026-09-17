-- Mutable manual review state is separate from immutable extraction artifacts/facts.
CREATE TABLE career_review_drafts (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL UNIQUE REFERENCES career_profiles(id),
    version INTEGER NOT NULL CHECK (version > 0),
    base_version_id TEXT REFERENCES profile_versions(id)
);

CREATE TABLE career_review_items (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES career_review_drafts(id),
    kind TEXT NOT NULL CHECK (kind IN ('experience', 'achievement', 'skill')),
    parent_id TEXT REFERENCES career_review_items(id),
    organization TEXT,
    role TEXT,
    date_range TEXT,
    summary TEXT,
    action_text TEXT,
    outcome_text TEXT,
    metric_value REAL,
    metric_unit TEXT,
    raw_skill_name TEXT,
    canonical_name TEXT,
    proficiency TEXT,
    -- Inline DraftEvidence references plus their document binding, not a new evidence store.
    sources_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('draft', 'needs_clarification', 'verified', 'rejected')),
    deleted INTEGER NOT NULL CHECK (deleted IN (0, 1)),
    dirty INTEGER NOT NULL CHECK (dirty IN (0, 1)),
    published_id TEXT,
    confirmed_at TEXT,
    CHECK ((kind = 'experience' AND parent_id IS NULL)
        OR (kind != 'experience' AND parent_id IS NOT NULL)),
    CHECK ((status = 'verified' AND confirmed_at IS NOT NULL)
        OR (status != 'verified' AND confirmed_at IS NULL))
);
CREATE INDEX idx_career_review_items_draft ON career_review_items(draft_id);

-- Preserve skill-specific provenance rather than borrowing a parent's assertion.
ALTER TABLE experience_evidence ADD COLUMN experience_skill_id TEXT
    REFERENCES experience_skills(id);
CREATE INDEX idx_experience_evidence_skill ON experience_evidence(experience_skill_id);
