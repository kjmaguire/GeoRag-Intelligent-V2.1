-- =============================================================================
-- Phase 0 — Layer E — Operational contract tables (master plan §35.1, §35.2)
--
-- Six tables that back the cross-cutting middleware in Step 5:
--   workspace.agent_timeouts          per-agent timeout + circuit breaker policy
--   workspace.prompt_versions         all prompt versions with promotion state
--   workspace.agent_prompt_pins       per-agent required prompt version
--   workspace.workspace_agent_config  per-workspace agent parameter overrides
--   workspace.idempotency_keys        idempotency tracking for R2+ agents
--   workspace.dry_run_outputs         R3+ dry-run output staging
-- =============================================================================

-- ---------------------------------------------------------------------------
-- workspace.agent_timeouts
--
-- Global per-agent policy. Phase 0 defaults: soft=30s, hard=120s, retries=1,
-- circuit_breaker=workspace, threshold=5, cool_down=300s. Tunable via
-- /admin/agent-config/timeouts.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workspace.agent_timeouts (
    agent_name              text        PRIMARY KEY,
    risk_tier               text        NOT NULL DEFAULT 'R0'
        CHECK (risk_tier IN ('R0','R1','R2','R3','R4','R5')),
    soft_timeout_ms         integer     NOT NULL DEFAULT 30000,
    hard_timeout_ms         integer     NOT NULL DEFAULT 120000,
    retry_count             smallint    NOT NULL DEFAULT 1,
    circuit_breaker_scope   text        NOT NULL DEFAULT 'workspace'
        CHECK (circuit_breaker_scope IN ('none','workspace','global')),
    failure_threshold       smallint    NOT NULL DEFAULT 5,
    cool_down_seconds       integer     NOT NULL DEFAULT 300,
    updated_at              timestamptz NOT NULL DEFAULT now(),
    updated_by              bigint      NULL,
    CONSTRAINT agent_timeouts_soft_lt_hard CHECK (soft_timeout_ms <= hard_timeout_ms)
);

COMMENT ON TABLE workspace.agent_timeouts IS
    'Per-agent timeout + retry + circuit-breaker policy. The wrapper (Step 5.1) reads from here on every invocation.';

-- ---------------------------------------------------------------------------
-- workspace.prompt_versions
--
-- All prompt versions ever staged. Promotion state tracks the lifecycle:
-- draft → staging → production → deprecated. The Prompt Release Approval
-- Agent (Phase 4) gates the staging→production transition.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workspace.prompt_versions (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    prompt_id       text        NOT NULL,                                       -- canonical prompt identifier (e.g. 'claim_validator', 'llm_incident_diagnosis')
    version         text        NOT NULL,                                       -- semver-ish (v0.1.0, v1.2.3-rc1)
    text            text        NOT NULL,
    parameters      jsonb       NOT NULL DEFAULT '{}'::jsonb,                   -- temperature, top_p, model_profile, etc.
    promotion_state text        NOT NULL DEFAULT 'draft'
        CHECK (promotion_state IN ('draft','staging','production','deprecated')),
    promoted_at     timestamptz NULL,
    deprecated_at   timestamptz NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      bigint      NULL,
    notes           text        NULL,
    CONSTRAINT prompt_versions_prompt_id_version UNIQUE (prompt_id, version)
);

-- At most one production version per prompt_id at a time.
CREATE UNIQUE INDEX IF NOT EXISTS prompt_versions_one_production_per_prompt
    ON workspace.prompt_versions (prompt_id) WHERE promotion_state = 'production';

CREATE INDEX IF NOT EXISTS prompt_versions_prompt_id_idx
    ON workspace.prompt_versions (prompt_id, created_at DESC);

COMMENT ON TABLE workspace.prompt_versions IS
    'Every prompt version ever authored, with promotion lifecycle. Wrapper resolves the active version via agent_prompt_pins or the production-promoted row.';

-- ---------------------------------------------------------------------------
-- workspace.agent_prompt_pins
--
-- Per-agent override pinning a specific prompt version. NULL pin = wrapper
-- falls through to the production-promoted prompt for that prompt_id.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workspace.agent_prompt_pins (
    agent_name          text        PRIMARY KEY,
    prompt_id           text        NOT NULL,
    prompt_version_id   uuid        NULL REFERENCES workspace.prompt_versions(id) ON DELETE SET NULL,
    pinned_at           timestamptz NULL,
    pinned_by           bigint      NULL,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE workspace.agent_prompt_pins IS
    'Per-agent prompt-version pin. NULL pin → wrapper resolves the production-promoted version.';

-- ---------------------------------------------------------------------------
-- workspace.workspace_agent_config
--
-- Per-workspace overrides of agent parameters. The wrapper merges:
--   global default → workspace_agent_config → invocation context
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workspace.workspace_agent_config (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid        NOT NULL REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
    agent_name      text        NOT NULL,
    config          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    enabled         boolean     NOT NULL DEFAULT true,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      bigint      NULL,
    CONSTRAINT workspace_agent_config_workspace_agent UNIQUE (workspace_id, agent_name)
);

COMMENT ON TABLE workspace.workspace_agent_config IS
    'Per-workspace overrides of agent parameters and enable/disable toggles.';

CREATE INDEX IF NOT EXISTS workspace_agent_config_workspace_idx
    ON workspace.workspace_agent_config (workspace_id);

-- ---------------------------------------------------------------------------
-- workspace.idempotency_keys
--
-- Risk-tier-aware idempotency dedupe. The wrapper computes the key per the
-- agent's risk tier and stores it alongside the components used:
--   R2: sha256(workspace_id || document_id || agent_name || agent_version)
--   R3: sha256(workspace_id || export_request_id || agent_name)
--   R4: sha256(workspace_id || sync_target || sync_request_id)
--   R5: sha256(workspace_id || target_id || signoff_session_id)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workspace.idempotency_keys (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    key_hash        bytea       NOT NULL UNIQUE,
    key_components  jsonb       NOT NULL,
    risk_tier       text        NOT NULL CHECK (risk_tier IN ('R2','R3','R4','R5')),
    workspace_id    uuid        NULL,
    agent_name      text        NOT NULL,
    agent_version   text        NULL,
    invocation_id   uuid        NULL,
    result_summary  jsonb       NULL,
    outcome         text        NULL CHECK (outcome IN ('success','refusal','failure','timeout','circuit_open')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NULL                                            -- NULL = never expires
);

COMMENT ON TABLE workspace.idempotency_keys IS
    'Idempotency dedupe for R2+ agent invocations. Lookup by key_hash for speed; key_components for auditability.';

CREATE INDEX IF NOT EXISTS idempotency_keys_workspace_agent_idx
    ON workspace.idempotency_keys (workspace_id, agent_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idempotency_keys_expires_idx
    ON workspace.idempotency_keys (expires_at) WHERE expires_at IS NOT NULL;

-- ---------------------------------------------------------------------------
-- workspace.dry_run_outputs
--
-- R3+ agents support a dry_run flag. Side-effects are intercepted by the
-- dry-run sink middleware (Step 5.4) and recorded here instead of executing.
-- The agent returns a dry_run_id pointing at the rows it staged.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workspace.dry_run_outputs (
    id                          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    invocation_id               uuid        NOT NULL,
    workspace_id                uuid        NOT NULL REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
    agent_name                  text        NOT NULL,
    target                      text        NOT NULL,                           -- e.g. 'sharepoint:archive', 'email:teams', 'qdrant:upsert'
    payload                     jsonb       NOT NULL,
    would_have_executed_at      timestamptz NOT NULL DEFAULT now(),
    created_at                  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE workspace.dry_run_outputs IS
    'Captured side-effect calls from dry-run agent invocations. None of these are executed.';

CREATE INDEX IF NOT EXISTS dry_run_outputs_invocation_idx
    ON workspace.dry_run_outputs (invocation_id);
CREATE INDEX IF NOT EXISTS dry_run_outputs_workspace_idx
    ON workspace.dry_run_outputs (workspace_id, created_at DESC);
