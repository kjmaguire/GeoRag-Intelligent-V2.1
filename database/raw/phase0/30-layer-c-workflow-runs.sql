-- =============================================================================
-- Phase 0 — Layer C — workflow.workflow_runs (partitioned) + workflow_run_events
--
-- The unified per-run record across Hatchet, Activepieces (Phase 2+),
-- LangGraph (Phase 4+), Dagster, and Laravel Horizon. Every orchestrator
-- writes here at run start, updates at status changes, and stamps trace_id
-- so OpenTelemetry / Tempo can be cross-referenced.
--
-- Partitioned monthly by started_at to keep query latency bounded as runs
-- accumulate over years.
-- =============================================================================

CREATE TABLE IF NOT EXISTS workflow.workflow_runs (
    run_id              uuid        NOT NULL DEFAULT gen_random_uuid(),
    workspace_id        uuid        NULL,                                       -- no FK, high-volume time-partitioned
    workflow_kind       text        NOT NULL,                                   -- e.g. 'ingest_pdf', 'audit_ledger_verify'
    engine              text        NOT NULL
        CHECK (engine IN ('hatchet','activepieces','langgraph','dagster','horizon','reverb')),
    engine_run_id       text        NULL,                                       -- the orchestrator's native run id
    status              text        NOT NULL
        CHECK (status IN ('queued','running','success','failure','cancelled','timed_out')),
    trace_id            text        NULL,                                       -- W3C Trace Context — links to Tempo
    started_at          timestamptz NOT NULL DEFAULT now(),
    ended_at            timestamptz NULL,
    duration_ms         bigint      GENERATED ALWAYS AS (
                            CASE WHEN ended_at IS NULL THEN NULL
                                 ELSE EXTRACT(EPOCH FROM (ended_at - started_at)) * 1000 END
                        ) STORED,
    input_summary       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    output_summary      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    failure_reason      jsonb       NULL,
    triggered_by        bigint      NULL,                                       -- public.users.id, no FK
    PRIMARY KEY (run_id, started_at)
) PARTITION BY RANGE (started_at);

COMMENT ON TABLE  workflow.workflow_runs IS
    'Unified workflow run record across all 5 orchestrators. Partitioned monthly by started_at.';
COMMENT ON COLUMN workflow.workflow_runs.engine_run_id IS
    'Native orchestrator id (Hatchet workflow run uuid, Activepieces flow run id, LangGraph thread id, etc.)';
COMMENT ON COLUMN workflow.workflow_runs.trace_id IS
    'W3C Trace Context trace_id — same value appears in Tempo span tree and Langfuse trace metadata.';

CREATE INDEX IF NOT EXISTS workflow_runs_workspace_id_idx
    ON workflow.workflow_runs (workspace_id, started_at DESC);
CREATE INDEX IF NOT EXISTS workflow_runs_kind_status_idx
    ON workflow.workflow_runs (workflow_kind, status, started_at DESC);
CREATE INDEX IF NOT EXISTS workflow_runs_engine_run_id_idx
    ON workflow.workflow_runs (engine, engine_run_id) WHERE engine_run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS workflow_runs_trace_id_idx
    ON workflow.workflow_runs (trace_id) WHERE trace_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- pg_partman: monthly partitions for workflow_runs
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM partman.part_config WHERE parent_table = 'workflow.workflow_runs') THEN
        PERFORM partman.create_parent(
            p_parent_table     := 'workflow.workflow_runs',
            p_control          := 'started_at',
            p_interval         := '1 month',
            p_premake          := 3,
            p_start_partition  := to_char(date_trunc('month', now()), 'YYYY-MM-DD')
        );
        UPDATE partman.part_config
            SET retention            = '24 months',
                retention_keep_table = true,
                infinite_time_partitions = true
            WHERE parent_table = 'workflow.workflow_runs';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- workflow.workflow_run_events
--
-- Per-step granularity within a run. step_name corresponds to the orchestrator
-- step (Hatchet step function, Dagster op, LangGraph node). Events accumulate
-- linearly within a run; total volume per run is typically 5–50 rows.
--
-- NOT partitioned at Phase 0 — volume is bounded; can be added in Phase 11
-- hardening if needed. event_type covers the lifecycle (started, succeeded,
-- failed, retried, timed_out) plus arbitrary log messages.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workflow.workflow_run_events (
    id              bigserial   PRIMARY KEY,
    run_id          uuid        NOT NULL,
    workspace_id    uuid        NULL,
    step_name       text        NULL,
    event_type      text        NOT NULL,
    payload         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    occurred_at     timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE  workflow.workflow_run_events IS
    'Per-step event log within a workflow run. Use trace_id from workflow_runs to correlate with Tempo spans.';

CREATE INDEX IF NOT EXISTS workflow_run_events_run_id_idx
    ON workflow.workflow_run_events (run_id, occurred_at);
CREATE INDEX IF NOT EXISTS workflow_run_events_workspace_id_idx
    ON workflow.workflow_run_events (workspace_id, occurred_at DESC) WHERE workspace_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS workflow_run_events_event_type_idx
    ON workflow.workflow_run_events (event_type, occurred_at DESC);
