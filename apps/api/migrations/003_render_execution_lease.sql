ALTER TABLE render_jobs
    ADD COLUMN execution_claimed_at TEXT;

CREATE INDEX IF NOT EXISTS idx_render_jobs_execution_lease
    ON render_jobs(owner_user_id, status, execution_claimed_at, created_at, id);
