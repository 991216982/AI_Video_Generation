PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS books (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS promotion_candidates (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    book_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (id, owner_user_id),
    FOREIGN KEY (book_id, owner_user_id) REFERENCES books(id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS creative_projects (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    book_id TEXT NOT NULL,
    promotion_candidate_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (id, owner_user_id),
    FOREIGN KEY (book_id, owner_user_id) REFERENCES books(id, owner_user_id),
    FOREIGN KEY (promotion_candidate_id, owner_user_id)
        REFERENCES promotion_candidates(id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS script_versions (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    version_number INTEGER NOT NULL,
    review_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (id, owner_user_id),
    UNIQUE (project_id, version_number),
    FOREIGN KEY (project_id, owner_user_id)
        REFERENCES creative_projects(id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    brief_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    review_resume_claimed_at TEXT,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (id, owner_user_id),
    UNIQUE (owner_user_id, project_id, idempotency_key),
    FOREIGN KEY (project_id, owner_user_id)
        REFERENCES creative_projects(id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS run_events (
    event_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (run_id, sequence),
    FOREIGN KEY (run_id, owner_user_id) REFERENCES runs(id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    script_version_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    FOREIGN KEY (run_id, owner_user_id) REFERENCES runs(id, owner_user_id),
    FOREIGN KEY (script_version_id, owner_user_id)
        REFERENCES script_versions(id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS render_jobs (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    script_version_id TEXT NOT NULL,
    promotion_candidate_id TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    asset_rights TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (id, owner_user_id),
    UNIQUE (owner_user_id, idempotency_key),
    FOREIGN KEY (project_id, owner_user_id)
        REFERENCES creative_projects(id, owner_user_id),
    FOREIGN KEY (script_version_id, owner_user_id)
        REFERENCES script_versions(id, owner_user_id),
    FOREIGN KEY (promotion_candidate_id, owner_user_id)
        REFERENCES promotion_candidates(id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS publication_packages (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    script_version_id TEXT NOT NULL,
    render_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (project_id, script_version_id, render_id),
    FOREIGN KEY (project_id, owner_user_id)
        REFERENCES creative_projects(id, owner_user_id),
    FOREIGN KEY (script_version_id, owner_user_id)
        REFERENCES script_versions(id, owner_user_id),
    FOREIGN KEY (render_id, owner_user_id)
        REFERENCES render_jobs(id, owner_user_id)
);
