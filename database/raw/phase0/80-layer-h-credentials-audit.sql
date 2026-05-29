-- =============================================================================
-- Phase 0 — Layer H — audit.integration_credentials_audit
--
-- OAuth/token lifecycle audit. Phase 0 ships the table; the Credential Health
-- Agent that writes here ships in Phase 2 (after Activepieces deploys and
-- there are real integrations to audit). NB: master plan v2.4.2 §30 has the
-- agent in Phase 0 but registry v1.3 correctly classifies it Phase 2 — see
-- Phase 0 kickoff §Step 7 Finding 2.
--
-- Cross-schema FKs: integration_id is intentionally a free-form text rather
-- than a FK because the integrations table itself ships in Phase 2 and
-- Phase 0 doesn't want a forward dependency.
-- =============================================================================

CREATE TABLE IF NOT EXISTS audit.integration_credentials_audit (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid        NULL,                                       -- no FK, audit table
    integration_id  text        NULL,                                       -- forward-compat with Phase 2 integrations.id
    integration_kind text       NOT NULL,                                   -- 'sharepoint','googledrive','slack','teams','arcgis_online','custom_oauth'
    action          text        NOT NULL
        CHECK (action IN ('created','refreshed','expired','rotated','revoked','failed_refresh','manual_reset')),
    credential_ref  text        NULL,                                       -- abstracted reference, never the secret itself
    expires_at      timestamptz NULL,                                       -- credential expiry recorded at this event
    actor_id        bigint      NULL,                                       -- public.users.id for human-triggered, NULL for system
    actor_kind      text        NOT NULL DEFAULT 'system'
        CHECK (actor_kind IN ('user','system','agent','integration')),
    payload         jsonb       NOT NULL DEFAULT '{}'::jsonb,               -- non-secret context: scopes, granted_by, error_kind, etc.
    occurred_at     timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE audit.integration_credentials_audit IS
    'OAuth/token lifecycle audit trail. Phase 0 deploys table; Phase 2 deploys agents that write to it.';
COMMENT ON COLUMN audit.integration_credentials_audit.credential_ref IS
    'Opaque pointer (e.g. integrations.id, KMS key alias). The secret material itself is never stored here.';
COMMENT ON COLUMN audit.integration_credentials_audit.payload IS
    'Non-secret context only: granted scopes, refresh interval, error categorisation, etc.';

CREATE INDEX IF NOT EXISTS integration_credentials_audit_workspace_idx
    ON audit.integration_credentials_audit (workspace_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS integration_credentials_audit_integration_idx
    ON audit.integration_credentials_audit (integration_id, occurred_at DESC) WHERE integration_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS integration_credentials_audit_action_idx
    ON audit.integration_credentials_audit (action, occurred_at DESC);
CREATE INDEX IF NOT EXISTS integration_credentials_audit_kind_idx
    ON audit.integration_credentials_audit (integration_kind, occurred_at DESC);
