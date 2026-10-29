ALTER TABLE promotion_candidates
    ADD COLUMN eligibility_revision INTEGER NOT NULL DEFAULT 1
    CHECK (eligibility_revision >= 1);
ALTER TABLE promotion_candidates
    ADD COLUMN eligibility_checked_by_user_id TEXT;

ALTER TABLE runs
    ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1);
ALTER TABLE runs
    ADD COLUMN cancel_requested_at TEXT;
ALTER TABLE runs
    ADD COLUMN execution_claimed_at TEXT;

ALTER TABLE render_jobs
    ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1);
ALTER TABLE render_jobs
    ADD COLUMN cancel_requested_at TEXT;
ALTER TABLE render_jobs
    ADD COLUMN cancel_reason TEXT;

CREATE TABLE IF NOT EXISTS idempotency_operations (
    owner_user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (owner_user_id, idempotency_key)
);

UPDATE promotion_candidates
SET payload = json_set(
    payload,
    '$.eligibility_revision', 1,
    '$.eligibility_checked_by_user_id',
    CASE
        WHEN json_extract(payload, '$.eligibility_status') = 'manually_confirmed'
        THEN owner_user_id
        ELSE NULL
    END
);

UPDATE runs
SET payload = json_set(
    payload,
    '$.attempt', 1,
    '$.cancel_requested_at', NULL
);

UPDATE render_jobs
SET payload = json_set(
    payload,
    '$.attempt', 1,
    '$.cancel_requested_at', NULL,
    '$.updated_at', updated_at
);
