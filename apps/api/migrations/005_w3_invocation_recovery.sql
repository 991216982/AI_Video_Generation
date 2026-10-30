CREATE TABLE IF NOT EXISTS generation_invocations (
    owner_user_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    invocation_key TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('started', 'completed', 'manual_reconciliation_required')),
    recovery_script TEXT,
    PRIMARY KEY (owner_user_id, invocation_key),
    FOREIGN KEY (run_id, owner_user_id) REFERENCES runs(id, owner_user_id)
);
