PRAGMA foreign_keys = ON;

-- W4 research desk: metric observations, content references, style cards, and CSV imports.
-- Every row stays owner-scoped; payloads mirror the v0.4 OpenAPI contract.

CREATE TABLE IF NOT EXISTS metric_observations (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS content_references (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS style_cards (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS import_jobs (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('preview', 'applied', 'partially_applied', 'failed')),
    file_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (id, owner_user_id)
);

CREATE INDEX IF NOT EXISTS idx_metric_observations_owner_list
    ON metric_observations (owner_user_id, collected_at, id);
CREATE INDEX IF NOT EXISTS idx_content_references_owner_list
    ON content_references (owner_user_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_style_cards_owner_list
    ON style_cards (owner_user_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_import_jobs_owner_list
    ON import_jobs (owner_user_id, created_at, id);

-- Backfill v0.3 evidence records that carried a metric into legacy_unclassified observations.
-- These keep the missing reason, are flagged legacy_backfill, and must not rank until re-validated.
INSERT INTO metric_observations (id, owner_user_id, collected_at, payload)
SELECT e.id, e.owner_user_id, e.collected_at,
    json_object(
        'id', e.id,
        'owner_user_id', e.owner_user_id,
        'collected_at', e.collected_at,
        'platform', NULL,
        'book_id', NULL,
        'object_type', 'book_edition',
        'object_ref', 'evidence:' || e.id,
        'object_label', NULL,
        'metric_name', CASE
            WHEN json_extract(e.payload, '$.metric_name') IN (
                'sales_units', 'sales_amount', 'listed_price', 'displayed_sales_label', 'plays',
                'likes', 'comments', 'shares', 'favorites', 'follower_count', 'ranking_position',
                'search_heat', 'other'
            ) THEN json_extract(e.payload, '$.metric_name')
            ELSE 'other'
        END,
        'metric_definition', 'Legacy v0.3 evidence metric; basis unclassified pending re-validation',
        'basis', 'legacy_unclassified',
        'unit', 'unspecified',
        'value', json_extract(e.payload, '$.metric_value'),
        'value_text', NULL,
        'window_start', json_extract(e.payload, '$.metric_window_start'),
        'window_end', json_extract(e.payload, '$.metric_window_end'),
        'observed_at', json_extract(e.payload, '$.observed_at'),
        'evidence_id', e.id,
        'missing_reason', json_extract(e.payload, '$.missing_reason'),
        'confidence', NULL,
        'expires_at', NULL,
        'note', 'Backfilled from v0.3 evidence; do not use for ranking until re-validated.',
        'legacy_backfill', TRUE
    )
FROM evidence e
WHERE json_extract(e.payload, '$.metric_name') IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM metric_observations m WHERE m.id = e.id
    );
