-- =============================================================================
-- Phase 0 — Layer B — audit_ledger (hash-chained, monthly-partitioned via pg_partman)
--                   + audit_ledger_verification_runs
--
-- The audit ledger is the system's tamper-evident record. Every state-changing
-- event writes one row; each row's `hash` is sha256 of its content + the
-- previous row's hash. The verification job (Step 4 — Hatchet workflow
-- audit_ledger_verify) walks the chain nightly.
--
-- Partitioning: monthly by created_at via pg_partman. Three months of
-- partitions are pre-created so the first month doesn't have to wait for the
-- background maintenance worker.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- audit.audit_ledger — partitioned parent
--
-- workspace_id is NOT a FK (per kickoff §Step 2 implementation notes — high-
-- volume time-partitioned tables avoid FKs to workspaces to reduce contention).
-- actor_id is bigint (matches public.users.id) — also no FK.
--
-- The hash chain is computed at INSERT time by a BEFORE-INSERT trigger
-- defined in 90-audit-hash-chain-trigger.sql.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit.audit_ledger (
    id              uuid        NOT NULL DEFAULT gen_random_uuid(),
    workspace_id    uuid        NULL,
    actor_id        bigint      NULL,
    actor_kind      text        NOT NULL DEFAULT 'user'
        CHECK (actor_kind IN ('user','system','agent','workflow','external')),
    action_type     text        NOT NULL,
    target_schema   text        NULL,
    target_table    text        NULL,
    target_id       text        NULL,
    payload         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    previous_hash   bytea       NULL,
    hash            bytea       NULL,
    trace_id        text        NULL,
    -- clock_timestamp() (not now()) so each row inside the same transaction
    -- gets a strictly increasing created_at — required for the hash-chain
    -- tiebreaker `ORDER BY created_at DESC, id DESC` to yield insertion order.
    -- (now() returns transaction-start time, which would tie all same-tx rows
    -- and let the random-UUID tiebreaker pick the wrong "previous" row.)
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

COMMENT ON TABLE  audit.audit_ledger IS
    'Hash-chained, monthly-partitioned audit trail. Every state-changing event writes here.';
COMMENT ON COLUMN audit.audit_ledger.action_type IS
    'Canonical: agent.invoke, workspace.create, report.signoff, storage.tier_transition, etc.';
COMMENT ON COLUMN audit.audit_ledger.hash IS
    'sha256(previous_hash || actor_id || action_type || target_schema || target_table || target_id || canonical_json(payload) || created_at_iso)';

CREATE INDEX IF NOT EXISTS audit_ledger_workspace_id_idx
    ON audit.audit_ledger (workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_ledger_action_type_idx
    ON audit.audit_ledger (action_type, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_ledger_target_idx
    ON audit.audit_ledger (target_schema, target_table, target_id);
CREATE INDEX IF NOT EXISTS audit_ledger_trace_id_idx
    ON audit.audit_ledger (trace_id) WHERE trace_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- pg_partman setup for audit_ledger
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM partman.part_config WHERE parent_table = 'audit.audit_ledger') THEN
        PERFORM partman.create_parent(
            p_parent_table     := 'audit.audit_ledger',
            p_control          := 'created_at',
            p_interval         := '1 month',
            p_premake          := 3,
            p_start_partition  := to_char(date_trunc('month', now()), 'YYYY-MM-DD')
        );
        UPDATE partman.part_config
            SET retention            = '24 months',
                retention_keep_table = true,
                infinite_time_partitions = true
            WHERE parent_table = 'audit.audit_ledger';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- audit.audit_ledger_verification_runs
--
-- One row per nightly hash-chain verification. status='clean' means the day
-- was verified end-to-end; 'break' means a hash mismatch was found and the
-- offending row(s) are listed in broken_ids.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit.audit_ledger_verification_runs (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    partition_date  date        NOT NULL,
    status          text        NOT NULL
        CHECK (status IN ('in_progress','clean','break','error')),
    rows_verified   bigint      NOT NULL DEFAULT 0,
    first_id        uuid        NULL,
    last_id         uuid        NULL,
    first_hash      bytea       NULL,
    last_hash       bytea       NULL,
    broken_ids      uuid[]      NULL,
    error_message   text        NULL,
    started_at      timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz NULL,
    workflow_run_id uuid        NULL
);

COMMENT ON TABLE  audit.audit_ledger_verification_runs IS
    'Per-day result of the audit_ledger_verify Hatchet workflow.';
COMMENT ON COLUMN audit.audit_ledger_verification_runs.broken_ids IS
    'Audit-ledger row IDs whose stored hash did not match recomputation. NULL when status=clean.';

CREATE INDEX IF NOT EXISTS audit_ledger_verification_runs_partition_date_idx
    ON audit.audit_ledger_verification_runs (partition_date DESC);
CREATE INDEX IF NOT EXISTS audit_ledger_verification_runs_status_idx
    ON audit.audit_ledger_verification_runs (status, started_at DESC);
