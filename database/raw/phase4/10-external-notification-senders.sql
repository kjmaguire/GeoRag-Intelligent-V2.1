-- =============================================================================
-- Phase 4 Step 1 — per-sender HMAC registry for external_notification.
--
-- Phase 3 Step 5 shipped HMAC sender authentication with a SINGLE shared
-- secret (env var EXTERNAL_NOTIFICATION_HMAC_SECRET). That works for one
-- sender; multi-sender support requires a per-sender secret with rotation.
--
-- Design:
--   - Each row in `usage.external_notification_senders` is one logical
--     sender (slack-app, partner-X, internal-cron, …) keyed by `source`.
--   - Secrets are stored encrypted-at-rest via pgcrypto's `pgp_sym_encrypt`,
--     using the cluster-wide `app.audit_encryption_key` GUC pattern from
--     Phase 0 §95. Decryption happens server-side inside the workflow.
--   - `secret_kid` lets a sender rotate without breaking in-flight
--     deliveries — both keys are checked during the rotation window.
--   - `disabled_at` is the kill switch (preferred over DELETE so the
--     audit trail of "what secret was active when" survives).
--
-- The Hatchet workflow (`external_notification.py`) reads this table
-- when verifying inbound HMAC signatures, falling back to the env-var
-- single-secret path during the co-existence window. Step 1 retro
-- decides when to retire the env-var fallback.
--
-- Idempotent.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage.external_notification_senders (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    source          text        NOT NULL,
    secret_kid      text        NOT NULL,                            -- key id; rotation cycles this
    secret_ciphertext bytea     NOT NULL,                            -- pgp_sym_encrypt() output
    description     text        NULL,
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    rotated_from_id uuid        NULL,                                -- FK chain when a rotation supersedes a prior key
    disabled_at     timestamptz NULL,
    last_seen_at    timestamptz NULL,                                -- updated by the workflow on first verify per row
    CONSTRAINT external_notification_senders_source_kid_unique UNIQUE (source, secret_kid),
    CONSTRAINT external_notification_senders_rotated_from_fkey
        FOREIGN KEY (rotated_from_id)
        REFERENCES usage.external_notification_senders(id)
        ON DELETE SET NULL
);

COMMENT ON TABLE  usage.external_notification_senders IS
    'Phase 4 Step 1 — per-sender HMAC registry for the external_notification flow. '
    'Replaces the single shared EXTERNAL_NOTIFICATION_HMAC_SECRET env var.';
COMMENT ON COLUMN usage.external_notification_senders.secret_ciphertext IS
    'pgp_sym_encrypt(secret_plaintext, current_setting(''app.audit_encryption_key''))';
COMMENT ON COLUMN usage.external_notification_senders.disabled_at IS
    'Kill switch. Disabled rows are returned by the lookup but rejected at verify time.';

CREATE INDEX IF NOT EXISTS external_notification_senders_source_idx
    ON usage.external_notification_senders (source)
    WHERE disabled_at IS NULL;
CREATE INDEX IF NOT EXISTS external_notification_senders_disabled_idx
    ON usage.external_notification_senders (disabled_at);

-- ---------------------------------------------------------------------------
-- 2. RLS — same workspace pattern, but senders are platform-scoped (NULL
-- workspace_id semantics — every admin sees every sender). Lock down
-- DELETE entirely; archival is via `disabled_at`.
-- ---------------------------------------------------------------------------
ALTER TABLE usage.external_notification_senders ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage.external_notification_senders FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS senders_admin_read ON usage.external_notification_senders;
CREATE POLICY senders_admin_read ON usage.external_notification_senders
    FOR SELECT
    USING (true);  -- platform-scoped; access gated at the application layer

DROP POLICY IF EXISTS senders_app_insert_update ON usage.external_notification_senders;
CREATE POLICY senders_app_insert_update ON usage.external_notification_senders
    FOR ALL
    USING (true)
    WITH CHECK (true);  -- mutations gated at the application layer

GRANT SELECT, INSERT, UPDATE ON usage.external_notification_senders TO georag_app;

-- ---------------------------------------------------------------------------
-- 3. Helper function — encrypt + insert a new sender or rotate an existing.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION usage.register_external_notification_sender(
    p_source       text,
    p_secret_kid   text,
    p_secret_plain text,
    p_description  text DEFAULT NULL,
    p_supersedes   uuid DEFAULT NULL
) RETURNS uuid
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = usage, public, pg_catalog
AS $$
DECLARE
    enc_key text := current_setting('app.audit_encryption_key', true);
    new_id  uuid;
BEGIN
    IF enc_key IS NULL OR enc_key = '' THEN
        RAISE EXCEPTION 'app.audit_encryption_key GUC not set — cannot encrypt sender secret';
    END IF;
    IF p_source = '' OR p_secret_kid = '' OR p_secret_plain = '' THEN
        RAISE EXCEPTION 'source, secret_kid, secret_plaintext are required';
    END IF;

    INSERT INTO usage.external_notification_senders
        (source, secret_kid, secret_ciphertext, description, rotated_from_id)
    VALUES (
        p_source,
        p_secret_kid,
        pgp_sym_encrypt(p_secret_plain, enc_key)::bytea,
        p_description,
        p_supersedes
    )
    RETURNING id INTO new_id;

    RETURN new_id;
END;
$$;

GRANT EXECUTE ON FUNCTION usage.register_external_notification_sender(text, text, text, text, uuid)
    TO georag_app;

-- ---------------------------------------------------------------------------
-- 4. Helper function — fetch the (possibly multiple, due to rotation) active
-- secret(s) for a source. Returns one row per active key.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION usage.lookup_external_notification_sender_secrets(
    p_source text
) RETURNS TABLE (sender_id uuid, secret_kid text, secret_plain text)
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = usage, public, pg_catalog
AS $$
DECLARE
    enc_key text := current_setting('app.audit_encryption_key', true);
BEGIN
    IF enc_key IS NULL OR enc_key = '' THEN
        RAISE EXCEPTION 'app.audit_encryption_key GUC not set — cannot decrypt sender secret';
    END IF;

    RETURN QUERY
        SELECT id,
               s.secret_kid,
               pgp_sym_decrypt(s.secret_ciphertext, enc_key)
          FROM usage.external_notification_senders s
         WHERE s.source = p_source
           AND s.disabled_at IS NULL
         ORDER BY s.created_at DESC;
END;
$$;

GRANT EXECUTE ON FUNCTION usage.lookup_external_notification_sender_secrets(text)
    TO georag_app;

-- ---------------------------------------------------------------------------
-- 5. Verification.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    n_table int;
    n_funcs int;
BEGIN
    SELECT count(*) INTO n_table FROM information_schema.tables
        WHERE table_schema='usage' AND table_name='external_notification_senders';
    SELECT count(*) INTO n_funcs FROM information_schema.routines
        WHERE routine_schema='usage'
          AND routine_name IN ('register_external_notification_sender',
                               'lookup_external_notification_sender_secrets');
    RAISE NOTICE 'Phase 4 Step 1: table=%, helper funcs=%', n_table, n_funcs;
    IF n_table <> 1 OR n_funcs <> 2 THEN
        RAISE EXCEPTION 'Phase 4 Step 1 install incomplete';
    END IF;
END $$;
