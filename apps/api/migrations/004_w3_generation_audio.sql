PRAGMA foreign_keys = ON;

ALTER TABLE promotion_candidates ADD COLUMN confirmation_basis TEXT;

CREATE TABLE IF NOT EXISTS book_fact_revisions (
    book_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    payload TEXT NOT NULL,
    PRIMARY KEY (book_id, fact_key, revision),
    FOREIGN KEY (book_id, owner_user_id) REFERENCES books(id, owner_user_id)
);

INSERT OR IGNORE INTO book_fact_revisions(book_id, owner_user_id, fact_key, revision, payload)
SELECT b.id, b.owner_user_id, json_extract(f.value, '$.key'), 1,
       json_object(
           'book_id', b.id,
           'key', json_extract(f.value, '$.key'),
           'revision', 1,
           'value', json_extract(f.value, '$.value'),
           'verification_status', json_extract(f.value, '$.verification_status'),
           'source_note', json_extract(f.value, '$.source_note'),
           'evidence_id', json_extract(f.value, '$.evidence_id'),
           'created_at', b.created_at,
           'created_by_user_id', b.owner_user_id
       )
FROM books AS b, json_each(b.payload, '$.facts') AS f;

UPDATE books SET payload = json_set(payload, '$.facts', json(
    (SELECT json_group_array(json_set(f.value, '$.revision', 1))
     FROM json_each(books.payload, '$.facts') AS f)));

UPDATE promotion_candidates
SET payload = json_set(payload,
    '$.eligibility_status', 'unverified',
    '$.eligibility_checked_at', NULL,
    '$.eligibility_checked_by_user_id', NULL,
    '$.eligibility_note', NULL,
    '$.eligibility_revision', eligibility_revision + 1),
    eligibility_revision = eligibility_revision + 1,
    eligibility_checked_by_user_id = NULL
WHERE json_extract(payload, '$.eligibility_status') = 'manually_confirmed'
  AND COALESCE(confirmation_basis, '') <> 'creator_account_backend';

CREATE TABLE IF NOT EXISTS audio_assets (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS generation_results (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    invocation_key TEXT NOT NULL,
    status TEXT NOT NULL,
    script_version_id TEXT,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (owner_user_id, invocation_key),
    UNIQUE (id, owner_user_id),
    FOREIGN KEY (run_id, owner_user_id) REFERENCES runs(id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS model_cost_records (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    generation_result_id TEXT NOT NULL UNIQUE,
    recorded_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    FOREIGN KEY (generation_result_id, owner_user_id)
        REFERENCES generation_results(id, owner_user_id)
);

ALTER TABLE render_jobs ADD COLUMN audio_asset_id TEXT;
UPDATE render_jobs SET payload = json_set(payload, '$.audio_asset_id', NULL);
