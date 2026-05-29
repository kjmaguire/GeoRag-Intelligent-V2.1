-- =============================================================================
-- Phase 0 — Layer G — silver findings tables (master plan §10.10, §22.1.1, §10.8)
--
-- Three tables that Phase 0 agents write into:
--   silver.store_reconciliation_findings  drift findings (Store Reconciliation Agent)
--   silver.corpus_health_findings         content-level findings (Corpus Health Agent — Phase 3 ships agent)
--   silver.storage_tier_policy            tier transition rules (Storage Tiering Agent)
-- =============================================================================

-- ---------------------------------------------------------------------------
-- silver.store_reconciliation_findings
--
-- Per-row drift between Postgres (the truth) and a secondary store. drift_type
-- is the kickoff-defined taxonomy — Phase 0 only has missing_in_b applicable
-- (no Qdrant/Neo4j yet); hash_mismatch + orphan_in_b come online in Phase 1.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.store_reconciliation_findings (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid        NOT NULL REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
    drift_type      text        NOT NULL
        CHECK (drift_type IN ('missing_in_b','orphan_in_b','hash_mismatch','stuck_propagation','outbox_dead_letter')),
    severity        text        NOT NULL DEFAULT 'medium'
        CHECK (severity IN ('critical','high','medium','low','info')),
    source_store    text        NOT NULL DEFAULT 'postgres',
    target_store    text        NOT NULL,
    source_id       text        NULL,
    target_id       text        NULL,
    details         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    status          text        NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','investigating','resolved','wontfix')),
    discovered_by   text        NULL,                                       -- agent name that found it
    discovered_at   timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz NULL,
    resolved_by     bigint      NULL,
    resolution_notes text       NULL
);

COMMENT ON TABLE silver.store_reconciliation_findings IS
    'Per-row drift findings from Store Reconciliation Agent (Phase 0 agent #5).';

CREATE INDEX IF NOT EXISTS store_reconciliation_findings_workspace_idx
    ON silver.store_reconciliation_findings (workspace_id, status, discovered_at DESC);
CREATE INDEX IF NOT EXISTS store_reconciliation_findings_open_severity_idx
    ON silver.store_reconciliation_findings (severity, discovered_at DESC) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS store_reconciliation_findings_drift_type_idx
    ON silver.store_reconciliation_findings (drift_type, target_store, discovered_at DESC);

-- ---------------------------------------------------------------------------
-- silver.corpus_health_findings
--
-- Content-level findings. Phase 0 ships the table empty so Phase 3's Corpus
-- Health Agent has somewhere to write at first run.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.corpus_health_findings (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid        NOT NULL REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
    finding_type    text        NOT NULL,                                   -- e.g. 'duplicate_passage','orphan_entity','stale_embedding','low_quality_ocr'
    severity        text        NOT NULL DEFAULT 'medium'
        CHECK (severity IN ('critical','high','medium','low','info')),
    target_schema   text        NULL,
    target_table    text        NULL,
    target_id       text        NULL,
    payload         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    status          text        NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','investigating','resolved','wontfix')),
    discovered_at   timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz NULL,
    resolved_by     bigint      NULL
);

COMMENT ON TABLE silver.corpus_health_findings IS
    'Content-level corpus findings. Phase 0 ships table; agent ships Phase 3.';

CREATE INDEX IF NOT EXISTS corpus_health_findings_workspace_idx
    ON silver.corpus_health_findings (workspace_id, status, discovered_at DESC);
CREATE INDEX IF NOT EXISTS corpus_health_findings_type_idx
    ON silver.corpus_health_findings (finding_type, discovered_at DESC);

-- ---------------------------------------------------------------------------
-- silver.storage_tier_policy
--
-- Per-workspace tier transition rules consumed by Storage Tiering Agent.
-- Defaults provisioned for the platform-level (workspace_id NULL) policy:
--   bronze raw       → warm at 30 days  → cold at 180 days
--   parser artifacts → cold at 60 days
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.storage_tier_policy (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id        uuid        NULL REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
    object_class        text        NOT NULL,                               -- e.g. 'bronze_raw','parser_artifact','export_bundle'
    source_tier         text        NOT NULL CHECK (source_tier IN ('hot','warm','cold')),
    target_tier         text        NOT NULL CHECK (target_tier IN ('hot','warm','cold')),
    age_threshold_days  integer     NOT NULL CHECK (age_threshold_days > 0),
    is_active           boolean     NOT NULL DEFAULT true,
    priority            smallint    NOT NULL DEFAULT 100,                   -- lower = applied first
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT storage_tier_policy_source_target_distinct CHECK (source_tier <> target_tier),
    CONSTRAINT storage_tier_policy_unique_per_scope
        UNIQUE (workspace_id, object_class, source_tier, target_tier)
);

COMMENT ON TABLE silver.storage_tier_policy IS
    'Tier transition rules per workspace × object class. workspace_id NULL = platform default.';

CREATE INDEX IF NOT EXISTS storage_tier_policy_workspace_active_idx
    ON silver.storage_tier_policy (workspace_id, is_active, priority);

-- Platform defaults (workspace_id NULL).
INSERT INTO silver.storage_tier_policy
    (workspace_id, object_class, source_tier, target_tier, age_threshold_days, priority)
VALUES
    (NULL, 'bronze_raw',       'hot',  'warm', 30,  100),
    (NULL, 'bronze_raw',       'warm', 'cold', 180, 110),
    (NULL, 'parser_artifact',  'hot',  'cold', 60,  120),
    (NULL, 'export_bundle',    'hot',  'warm', 14,  130),
    (NULL, 'export_bundle',    'warm', 'cold', 90,  140)
ON CONFLICT (workspace_id, object_class, source_tier, target_tier) DO NOTHING;
