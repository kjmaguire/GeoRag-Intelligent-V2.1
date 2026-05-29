-- =============================================================================
-- Phase 3 Step 3 — rename flow feature flags to a neutral namespace.
--
-- Phase 2 used `activepieces.<flow>.enabled`; Phase 3 introduces Kestra
-- alongside Activepieces during the migration window, so the flag
-- namespace shifts to the orchestrator-agnostic `flows.<flow>.enabled`.
--
-- Strategy: COPY values forward to the new key but DON'T drop the old
-- rows yet — Phase 3 Step 7 (Activepieces sunset) drops them after the
-- migration is fully cut over. During Steps 3–6 both rows exist; the
-- Hatchet workflows + IntegrationsController read the new key only,
-- so the new key is authoritative.
--
-- Idempotent.
-- =============================================================================

-- 1. Mirror activepieces.public_geoscience_pull.enabled → flows.*
INSERT INTO workspace.feature_flags
    (workspace_id, flag_name, bool_value, description, created_at, updated_at)
SELECT
    workspace_id,
    'flows.public_geoscience_pull.enabled' AS flag_name,
    bool_value,
    'Phase 3 Step 3 — gate the public_geoscience_pull Hatchet workflow. '
    'Orchestrator-agnostic name (replaces activepieces.public_geoscience_pull.enabled).' AS description,
    now(),
    now()
FROM workspace.feature_flags
WHERE flag_name = 'activepieces.public_geoscience_pull.enabled'
  AND NOT EXISTS (
    SELECT 1 FROM workspace.feature_flags ff2
     WHERE ff2.flag_name = 'flows.public_geoscience_pull.enabled'
       AND ff2.workspace_id IS NOT DISTINCT FROM workspace.feature_flags.workspace_id
  );

-- Seed the platform default if neither old nor new exists.
INSERT INTO workspace.feature_flags
    (workspace_id, flag_name, bool_value, description)
VALUES
    (NULL, 'flows.public_geoscience_pull.enabled', false,
     'Phase 3 Step 3 — gate the public_geoscience_pull Hatchet workflow. '
     'When false the workflow returns skipped=true without writing bronze.provenance or audit.')
ON CONFLICT (workspace_id, flag_name) DO NOTHING;

-- 2. Mirror activepieces.external_notification.enabled → flows.*
INSERT INTO workspace.feature_flags
    (workspace_id, flag_name, bool_value, description, created_at, updated_at)
SELECT
    workspace_id,
    'flows.external_notification.enabled' AS flag_name,
    bool_value,
    'Phase 3 Step 3 — gate the external_notification Hatchet workflow. '
    'Orchestrator-agnostic name (replaces activepieces.external_notification.enabled).' AS description,
    now(),
    now()
FROM workspace.feature_flags
WHERE flag_name = 'activepieces.external_notification.enabled'
  AND NOT EXISTS (
    SELECT 1 FROM workspace.feature_flags ff2
     WHERE ff2.flag_name = 'flows.external_notification.enabled'
       AND ff2.workspace_id IS NOT DISTINCT FROM workspace.feature_flags.workspace_id
  );

INSERT INTO workspace.feature_flags
    (workspace_id, flag_name, bool_value, description)
VALUES
    (NULL, 'flows.external_notification.enabled', false,
     'Phase 3 Step 3 — gate the external_notification Hatchet workflow. '
     'When false the workflow returns skipped=true without writing the audit row.')
ON CONFLICT (workspace_id, flag_name) DO NOTHING;

DO $$
DECLARE
    n_old int;
    n_new int;
BEGIN
    SELECT count(*) INTO n_old FROM workspace.feature_flags
        WHERE flag_name LIKE 'activepieces.%.enabled';
    SELECT count(*) INTO n_new FROM workspace.feature_flags
        WHERE flag_name LIKE 'flows.%.enabled';
    RAISE NOTICE 'flag rename: activepieces.* rows=% (kept until Step 7), flows.* rows=%',
                 n_old, n_new;
END $$;
