-- =============================================================================
-- Phase 5 Step 2 — per-flow JWT signing keys (R-P4-4).
--
-- Adds optional per-flow JWT signing secrets to `workflow.flow_registry`.
-- Each flow can have its own HS256 key + key-id; if absent, the workflow
-- falls back to the shared `KESTRA_FLOW_JWT_SECRET` env var (Phase 3
-- Step 3 behavior, unchanged).
--
-- Storage: encrypted-at-rest via pgcrypto's `pgp_sym_encrypt`, using the
-- same `app.audit_encryption_key` GUC pattern as the per-sender HMAC
-- registry (Phase 4 Step 1). The shared encryption key bridges the two
-- tables so operators rotate ONE secret to re-encrypt both.
--
-- Idempotent.
-- =============================================================================

ALTER TABLE workflow.flow_registry
    ADD COLUMN IF NOT EXISTS jwt_secret_kid       text  NULL,
    ADD COLUMN IF NOT EXISTS jwt_secret_ciphertext bytea NULL;

COMMENT ON COLUMN workflow.flow_registry.jwt_secret_kid IS
    'Phase 5 Step 2 — per-flow JWT key id (kid claim). NULL = use env-var fallback.';
COMMENT ON COLUMN workflow.flow_registry.jwt_secret_ciphertext IS
    'pgp_sym_encrypt(secret_plaintext, current_setting(''app.audit_encryption_key''))';

-- ---------------------------------------------------------------------------
-- Setter — operator-facing mint helper. Generates a fresh secret OR
-- accepts a caller-supplied plaintext (for cross-environment migration).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION workflow.set_flow_jwt_secret(
    p_flow_name    text,
    p_secret_kid   text,
    p_secret_plain text
) RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = workflow, public, pg_catalog
AS $$
DECLARE
    enc_key text := current_setting('app.audit_encryption_key', true);
BEGIN
    IF enc_key IS NULL OR enc_key = '' THEN
        RAISE EXCEPTION 'app.audit_encryption_key GUC not set';
    END IF;
    IF p_secret_kid = '' OR p_secret_plain = '' THEN
        RAISE EXCEPTION 'secret_kid + secret_plaintext required';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM workflow.flow_registry WHERE flow_name = p_flow_name) THEN
        RAISE EXCEPTION 'unknown flow_name: %', p_flow_name;
    END IF;

    UPDATE workflow.flow_registry
       SET jwt_secret_kid        = p_secret_kid,
           jwt_secret_ciphertext = pgp_sym_encrypt(p_secret_plain, enc_key)::bytea,
           updated_at            = clock_timestamp()
     WHERE flow_name = p_flow_name;
END;
$$;

GRANT EXECUTE ON FUNCTION workflow.set_flow_jwt_secret(text, text, text) TO georag_app;

-- ---------------------------------------------------------------------------
-- Getter — returns (kid, plain) or empty if no per-flow key. Loader on
-- the FastAPI side calls this on each verify; cached in process for the
-- registry TTL (60s).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION workflow.get_flow_jwt_secret(p_flow_name text)
RETURNS TABLE (kid text, plain text)
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = workflow, public, pg_catalog
AS $$
DECLARE
    enc_key text := current_setting('app.audit_encryption_key', true);
BEGIN
    IF enc_key IS NULL OR enc_key = '' THEN
        RAISE EXCEPTION 'app.audit_encryption_key GUC not set';
    END IF;
    RETURN QUERY
        SELECT r.jwt_secret_kid,
               pgp_sym_decrypt(r.jwt_secret_ciphertext, enc_key)
          FROM workflow.flow_registry r
         WHERE r.flow_name = p_flow_name
           AND r.jwt_secret_kid IS NOT NULL
           AND r.jwt_secret_ciphertext IS NOT NULL;
END;
$$;

GRANT EXECUTE ON FUNCTION workflow.get_flow_jwt_secret(text) TO georag_app;

DO $$
DECLARE
    n_col int;
    n_fn  int;
BEGIN
    SELECT count(*) INTO n_col FROM information_schema.columns
     WHERE table_schema='workflow' AND table_name='flow_registry'
       AND column_name IN ('jwt_secret_kid','jwt_secret_ciphertext');
    -- Count DISTINCT names (not signatures) so re-applies after later
    -- phases (Phase 6 Step 3 adds an overlap_hours overload of
    -- set_flow_jwt_secret) don't spuriously double-count.
    SELECT count(DISTINCT routine_name) INTO n_fn FROM information_schema.routines
     WHERE routine_schema='workflow'
       AND routine_name IN ('set_flow_jwt_secret','get_flow_jwt_secret');
    RAISE NOTICE 'Phase 5 Step 2: cols=% fns=%', n_col, n_fn;
    IF n_col <> 2 OR n_fn <> 2 THEN
        RAISE EXCEPTION 'Phase 5 Step 2 install incomplete';
    END IF;
END $$;
