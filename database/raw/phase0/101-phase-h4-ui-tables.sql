-- =============================================================================
-- Phase H4 UI tables — promoted from runtime CREATE IF NOT EXISTS DDL.
--
-- These tables back the Phase H4 admin pages:
--   silver.qp_credentials       — §29.6 QP credential registry
--   silver.workspace_settings   — per-workspace prefs (tone, defaults, SLA)
--   workflow.activepieces_channels — outbox dispatcher channel registry
--
-- The router code in app/routers/admin_tier234.py also performs the same
-- CREATE IF NOT EXISTS on first call so dev/test environments work without
-- running this migration; production deploys should run this file once for
-- a clean install with grants + RLS in place.
--
-- Idempotent.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- silver.qp_credentials — Qualified Person credential registry (§29.6).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.qp_credentials (
    qp_credential_id     text       PRIMARY KEY,
    user_id              integer    NOT NULL,
    name                 text       NOT NULL,
    issuing_body         text       NOT NULL,   -- APGO / EGBC / PEGNL / etc.
    registration_number  text       NOT NULL,
    jurisdiction         text       NOT NULL,
    expires_at           timestamptz,
    verified_at          timestamptz,
    is_active            boolean    NOT NULL DEFAULT true,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_qp_credentials_user_id
    ON silver.qp_credentials (user_id);
CREATE INDEX IF NOT EXISTS idx_qp_credentials_verified
    ON silver.qp_credentials (verified_at)
    WHERE verified_at IS NOT NULL;

COMMENT ON TABLE silver.qp_credentials IS
    'Phase H4 §29.6 — QP credential registry referenced by the R5 sign-off ceremony.';

-- Cross-workspace registry (not RLS-scoped — QPs sign off across workspaces).
-- Access is gated at the Laravel ''admin'' Gate.

-- ---------------------------------------------------------------------------
-- silver.workspace_settings — per-workspace preferences (Phase H4).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.workspace_settings (
    workspace_id           uuid       PRIMARY KEY REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
    default_tone           text       NOT NULL DEFAULT 'technical'
        CHECK (default_tone IN ('technical', 'executive', 'regulator')),
    default_report_type    text,
    sla_max_response_ms    integer    CHECK (sla_max_response_ms IS NULL OR sla_max_response_ms > 0),
    extra_payload          jsonb      NOT NULL DEFAULT '{}'::jsonb,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_workspace_settings_workspace_id
    ON silver.workspace_settings (workspace_id);

COMMENT ON TABLE silver.workspace_settings IS
    'Phase H4 — per-workspace UI/agent preferences. default_tone drives §7.6 Presentation Coach.';

ALTER TABLE silver.workspace_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE silver.workspace_settings FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workspace_settings_workspace_isolation ON silver.workspace_settings;
CREATE POLICY workspace_settings_workspace_isolation ON silver.workspace_settings
    USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);

-- ---------------------------------------------------------------------------
-- workflow.activepieces_channels — outbox dispatcher channel registry.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workflow.activepieces_channels (
    channel       text       PRIMARY KEY,
    webhook_url   text       NOT NULL,
    hmac_kid      text,
    is_active     boolean    NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_activepieces_channels_active
    ON workflow.activepieces_channels (channel)
    WHERE is_active = true;

COMMENT ON TABLE workflow.activepieces_channels IS
    'Phase H4 — per-channel Activepieces webhook URL + HMAC kid. The outbox '
    'dispatcher falls back to ACTIVEPIECES_WEBHOOK_URL_DEFAULT env var when a '
    'channel has no row.';

-- workflow.* is platform-level (cross-tenant by design). No RLS — access
-- gated at the Laravel ''admin'' Gate.

COMMIT;
