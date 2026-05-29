-- =============================================================================
-- Phase 4 Step 4 — DB-driven flow registry.
--
-- Single source of truth for the integration flow catalog. Replaces:
--   - FastAPI:  src/fastapi/app/routers/integrations_trigger.FLOW_REGISTRY
--   - Laravel:  IntegrationsController::REGISTERED_FLOWS
--
-- Both sides read this table on every cache TTL window (60s); adding a new
-- flow is `INSERT INTO workflow.flow_registry …`, not a code deploy.
--
-- Schema fields:
--   flow_name              — URL path segment for /internal/v1/integrations/<>/trigger
--   kind                   — operational category: 'scheduled-import',
--                            'inbound-webhook', 'placeholder', etc.
--   description            — operator-facing text shown on /admin/integrations
--   hatchet_workflow_module — Python module path for the workflow object
--                            (e.g. 'app.hatchet_workflows.public_geoscience_pull')
--   hatchet_workflow_attr  — attribute name on that module (e.g.
--                            'public_geoscience_pull' — the workflow itself)
--   pydantic_input_attr    — attribute name on the same module for the
--                            Pydantic input model class
--   flag_name              — feature flag in workspace.feature_flags
--                            (e.g. 'flows.public_geoscience_pull.enabled');
--                            NULL means flow runs unconditionally
--   enabled                — top-level toggle independent of the flag; flow
--                            is effectively retired when enabled=false
--   created_at, updated_at — bookkeeping
--
-- Idempotent. Seeds the three current flows from the hard-coded registries.
-- =============================================================================

CREATE TABLE IF NOT EXISTS workflow.flow_registry (
    flow_name               text        PRIMARY KEY,
    kind                    text        NOT NULL,
    description             text        NOT NULL,
    hatchet_workflow_module text        NOT NULL,
    hatchet_workflow_attr   text        NOT NULL,
    pydantic_input_attr     text        NOT NULL,
    flag_name               text        NULL,
    enabled                 boolean     NOT NULL DEFAULT true,
    created_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT flow_registry_kind_check CHECK (
        kind IN ('scheduled-import', 'inbound-webhook', 'placeholder', 'agent-trigger')
    ),
    CONSTRAINT flow_registry_flag_name_format CHECK (
        flag_name IS NULL OR flag_name ~ '^flows\.[a-z0-9_]+\.enabled$'
    )
);

COMMENT ON TABLE  workflow.flow_registry IS
    'Phase 4 Step 4 — single source of truth for integration flow catalog. '
    'Both FastAPI and Laravel read this; adding a flow is INSERT not code deploy.';

CREATE INDEX IF NOT EXISTS flow_registry_enabled_idx ON workflow.flow_registry (enabled);

-- ---------------------------------------------------------------------------
-- Touch updated_at on UPDATE.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION workflow.flow_registry_touch_updated_at()
    RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS flow_registry_touch_updated_at ON workflow.flow_registry;
CREATE TRIGGER flow_registry_touch_updated_at
    BEFORE UPDATE ON workflow.flow_registry
    FOR EACH ROW EXECUTE FUNCTION workflow.flow_registry_touch_updated_at();

GRANT SELECT, INSERT, UPDATE ON workflow.flow_registry TO georag_app;

-- ---------------------------------------------------------------------------
-- Seed: mirror the three currently-hard-coded flows.
-- ---------------------------------------------------------------------------
INSERT INTO workflow.flow_registry
    (flow_name, kind, description, hatchet_workflow_module,
     hatchet_workflow_attr, pydantic_input_attr, flag_name, enabled)
VALUES
    (
        'phase2_smoke',
        'placeholder',
        'Connectivity-debug echo workflow. Triggerable for ops smoke; not driven by any Kestra flow.',
        'app.hatchet_workflows.phase2_smoke',
        'phase2_smoke',
        'Phase2SmokeInput',
        NULL,
        true
    ),
    (
        'public_geoscience_pull',
        'scheduled-import',
        'Cron pulls a public geoscience feed → S3 → records bronze.provenance.',
        'app.hatchet_workflows.public_geoscience_pull',
        'public_geoscience_pull',
        'PublicGeoSciencePullInput',
        'flows.public_geoscience_pull.enabled',
        true
    ),
    (
        'external_notification',
        'inbound-webhook',
        'External sender posts to an orchestrator webhook → idempotent record in audit_ledger.',
        'app.hatchet_workflows.external_notification',
        'external_notification',
        'ExternalNotificationInput',
        'flows.external_notification.enabled',
        true
    )
ON CONFLICT (flow_name) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Verification.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    n int;
BEGIN
    SELECT count(*) INTO n FROM workflow.flow_registry;
    RAISE NOTICE 'Phase 4 Step 4: flow_registry rows = %', n;
    IF n < 3 THEN
        RAISE EXCEPTION 'Phase 4 Step 4 seed incomplete: got % (expected >= 3)', n;
    END IF;
END $$;
