-- =============================================================================
-- Phase 0 — Layer F — Usage & cost (master plan §23.6.1, §35.1)
--
-- Three tables for cost attribution + ceiling enforcement:
--   usage.usage_events          per-LLM-call cost attribution (partitioned)
--   usage.usage_aggregates_daily daily roll-ups for billing dashboards
--   usage.workspace_cost_ceilings soft-warn / hard-stop thresholds per workspace
--
-- The wrapper (Step 5.1) writes one usage_events row per agent invocation
-- that calls the LLM. The Model Cost Summary Agent (Phase 0 agent #9) rolls
-- the events into usage_aggregates_daily nightly.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- usage.usage_events — partitioned monthly by created_at
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage.usage_events (
    id                      bigserial,
    workspace_id            uuid        NULL,                       -- no FK, high-volume time-partitioned
    agent_name              text        NOT NULL,
    agent_version           text        NULL,
    model_profile           text        NOT NULL,                   -- e.g. 'chat_deep', 'chat_fast', 'embed_query'
    model_id                text        NULL,                       -- vLLM-served model id (for change-tracking)
    tokens_prompt           integer     NOT NULL DEFAULT 0,
    tokens_completion       integer     NOT NULL DEFAULT 0,
    tokens_total            integer     GENERATED ALWAYS AS (tokens_prompt + tokens_completion) STORED,
    projected_cost_usd      numeric(12, 6) NOT NULL DEFAULT 0,
    latency_ms              integer     NULL,
    outcome                 text        NOT NULL DEFAULT 'success'
        CHECK (outcome IN ('success','refusal','failure','timeout','circuit_open')),
    trace_id                text        NULL,
    invocation_id           uuid        NULL,
    parent_workflow_run_id  uuid        NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

COMMENT ON TABLE usage.usage_events IS
    'Per-LLM-call cost attribution. One row per invocation that touches vLLM.';
COMMENT ON COLUMN usage.usage_events.projected_cost_usd IS
    'Projected USD based on per-model pricing config; treated as informational since vLLM serving is internal.';

CREATE INDEX IF NOT EXISTS usage_events_workspace_idx
    ON usage.usage_events (workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS usage_events_agent_idx
    ON usage.usage_events (agent_name, created_at DESC);
CREATE INDEX IF NOT EXISTS usage_events_trace_id_idx
    ON usage.usage_events (trace_id) WHERE trace_id IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM partman.part_config WHERE parent_table = 'usage.usage_events') THEN
        PERFORM partman.create_parent(
            p_parent_table     := 'usage.usage_events',
            p_control          := 'created_at',
            p_interval         := '1 month',
            p_premake          := 3,
            p_start_partition  := to_char(date_trunc('month', now()), 'YYYY-MM-DD')
        );
        UPDATE partman.part_config
            SET retention            = '24 months',
                retention_keep_table = true,
                infinite_time_partitions = true
            WHERE parent_table = 'usage.usage_events';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- usage.usage_aggregates_daily
--
-- Materialized daily roll-ups. Composite PK lets the rollup job UPSERT
-- without having to delete/recreate.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage.usage_aggregates_daily (
    workspace_id            uuid        NOT NULL,
    agent_name              text        NOT NULL,
    model_profile           text        NOT NULL,
    rollup_date             date        NOT NULL,
    invocations_total       bigint      NOT NULL DEFAULT 0,
    invocations_success     bigint      NOT NULL DEFAULT 0,
    invocations_failure     bigint      NOT NULL DEFAULT 0,
    tokens_prompt_total     bigint      NOT NULL DEFAULT 0,
    tokens_completion_total bigint      NOT NULL DEFAULT 0,
    cost_usd_total          numeric(14, 6) NOT NULL DEFAULT 0,
    last_updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, agent_name, model_profile, rollup_date)
);

COMMENT ON TABLE usage.usage_aggregates_daily IS
    'Daily UPSERT roll-up of usage_events, written by the Model Cost Summary Agent.';

CREATE INDEX IF NOT EXISTS usage_aggregates_daily_workspace_date_idx
    ON usage.usage_aggregates_daily (workspace_id, rollup_date DESC);

-- ---------------------------------------------------------------------------
-- usage.workspace_cost_ceilings
--
-- Per-workspace monthly USD ceiling. soft_warn fires at threshold_pct of
-- ceiling; hard_stop refuses further LLM calls (with admin override).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage.workspace_cost_ceilings (
    workspace_id                uuid        PRIMARY KEY REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
    monthly_ceiling_usd         numeric(12, 2) NOT NULL,
    soft_warn_threshold_pct     smallint    NOT NULL DEFAULT 80
        CHECK (soft_warn_threshold_pct BETWEEN 1 AND 100),
    hard_stop_threshold_pct     smallint    NOT NULL DEFAULT 100
        CHECK (hard_stop_threshold_pct BETWEEN 1 AND 200),
    admin_override_enabled      boolean     NOT NULL DEFAULT false,
    admin_override_expires_at   timestamptz NULL,
    last_warn_sent_at           timestamptz NULL,
    last_warn_pct               smallint    NULL,
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    updated_by                  bigint      NULL,
    CONSTRAINT workspace_cost_ceilings_thresholds_ordered
        CHECK (soft_warn_threshold_pct <= hard_stop_threshold_pct)
);

COMMENT ON TABLE usage.workspace_cost_ceilings IS
    'Per-workspace monthly USD ceiling with soft-warn + hard-stop thresholds. Phase 0 plumbs storage; enforcement gate ships in Phase 4.';
