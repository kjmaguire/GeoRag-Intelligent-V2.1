-- =============================================================================
-- Phase 2 Step 4 — feature-flag seeds for Activepieces flows.
--
-- Per D5 in the scope proposal: every Activepieces flow gates on
-- `activepieces.<flow_name>.enabled` (boolean). Default is **false**;
-- operators flip on after the first manual smoke succeeds. Reuse the
-- workspace.feature_flags + feature_flag_history mechanics from
-- Phase 1 R-P1-6 — no new infra.
--
-- Idempotent.
-- =============================================================================

INSERT INTO workspace.feature_flags
    (workspace_id, flag_name, bool_value, description)
VALUES
    (NULL, 'activepieces.public_geoscience_pull.enabled', false,
     'Phase 2 Step 4 — gate the public_geoscience_pull Hatchet workflow. '
     'Activepieces fires this flow on cron; while disabled the workflow '
     'returns skipped=true without writing bronze.provenance or audit. '
     'Flip via /admin/integrations or `INSERT … ON CONFLICT DO UPDATE`.'),
    (NULL, 'activepieces.external_notification.enabled', false,
     'Phase 2 Step 5a — gate the external_notification Hatchet workflow. '
     'Activepieces exposes a webhook URL; inbound POSTs are forwarded here. '
     'While disabled the workflow returns skipped=true without writing the '
     'audit row. Flip via /admin/integrations.')
ON CONFLICT (workspace_id, flag_name) DO NOTHING;

DO $$
DECLARE n int;
BEGIN
    SELECT count(*) INTO n FROM workspace.feature_flags
        WHERE flag_name = 'activepieces.public_geoscience_pull.enabled';
    RAISE NOTICE 'activepieces flow flags seeded: %', n;
END $$;
