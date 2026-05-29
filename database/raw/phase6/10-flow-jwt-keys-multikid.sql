-- =============================================================================
-- Phase 6 Step 3 — multi-kid per-flow JWT signing keys (R-P5-2).
--
-- Phase 5 Step 2 added `jwt_secret_kid` + `jwt_secret_ciphertext` to
-- `workflow.flow_registry`, one kid per flow. Rotating overwrites the
-- old kid → every in-flight JWT signed with the old key fails verify
-- the moment the new kid lands. For real rotation in production we
-- need an overlap window where BOTH kids verify.
--
-- This migration:
--   1. Adds `workflow.flow_jwt_keys (flow_name, kid, ciphertext,
--      valid_from, valid_until, created_at)` — one row per historical
--      kid. NULL `valid_until` = "active until rotated out".
--   2. Backfills existing `flow_registry.jwt_secret_*` rows.
--   3. Rewrites `set_flow_jwt_secret()` to take an optional
--      `overlap_hours` parameter:
--         - With overlap=0 (default): hard-replace the kid (drops the
--           old kid immediately). Same shape as Phase 5 Step 2.
--         - With overlap>0: set the prior kid's `valid_until = now()
--           + overlap_hours` so both keys verify during the window.
--   4. Rewrites `get_flow_jwt_secret()` to return the ACTIVE-FOR-MINT
--      row (most recent kid where now() is in its valid window).
--   5. Adds `get_flow_jwt_keys(flow_name, ts)` returning every kid
--      whose window includes `ts` — used by the verify path.
--
-- The Phase 5 columns on `flow_registry` (`jwt_secret_kid`,
-- `jwt_secret_ciphertext`) become a denormalised view of the active
-- kid; they stay populated for the back-compat one-kid loader path
-- but are derived from `flow_jwt_keys` going forward.
--
-- Idempotent.
-- =============================================================================

CREATE TABLE IF NOT EXISTS workflow.flow_jwt_keys (
    id           uuid           PRIMARY KEY DEFAULT gen_random_uuid(),
    flow_name    text           NOT NULL REFERENCES workflow.flow_registry(flow_name) ON DELETE CASCADE,
    kid          text           NOT NULL,
    ciphertext   bytea          NOT NULL,
    valid_from   timestamptz    NOT NULL DEFAULT clock_timestamp(),
    valid_until  timestamptz    NULL,
    created_at   timestamptz    NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT flow_jwt_keys_unique_kid UNIQUE (flow_name, kid),
    CONSTRAINT flow_jwt_keys_window_check
        CHECK (valid_until IS NULL OR valid_until > valid_from)
);

CREATE INDEX IF NOT EXISTS flow_jwt_keys_flow_active_idx
    ON workflow.flow_jwt_keys (flow_name, valid_from DESC)
    WHERE valid_until IS NULL;

CREATE INDEX IF NOT EXISTS flow_jwt_keys_flow_window_idx
    ON workflow.flow_jwt_keys (flow_name, valid_from, valid_until);

COMMENT ON TABLE workflow.flow_jwt_keys IS
    'Phase 6 Step 2 — per-flow JWT signing keys with overlap-window rotation. '
    'NULL valid_until means "active until rotated out". A flow may have '
    'multiple rows whose valid windows overlap; verify accepts any kid '
    'in the active set, mint picks the most recently created.';

-- Backfill from Phase 5 Step 2's one-kid-per-flow columns.
INSERT INTO workflow.flow_jwt_keys (flow_name, kid, ciphertext, valid_from, valid_until)
SELECT r.flow_name,
       r.jwt_secret_kid,
       r.jwt_secret_ciphertext,
       COALESCE(r.updated_at, clock_timestamp()),
       NULL
  FROM workflow.flow_registry r
 WHERE r.jwt_secret_kid IS NOT NULL
   AND r.jwt_secret_ciphertext IS NOT NULL
   AND NOT EXISTS (
       SELECT 1 FROM workflow.flow_jwt_keys k
        WHERE k.flow_name = r.flow_name AND k.kid = r.jwt_secret_kid
   );

-- ---------------------------------------------------------------------------
-- Setter — provision a new kid, optionally retiring the prior kid after
-- an overlap window. Drop the Phase 5 three-arg signature first to
-- prevent ambiguous overload resolution (Postgres dispatches by full
-- argument list; the optional default arg would create two candidates
-- for callers passing 3 args).
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS workflow.set_flow_jwt_secret(text, text, text);

CREATE OR REPLACE FUNCTION workflow.set_flow_jwt_secret(
    p_flow_name     text,
    p_secret_kid    text,
    p_secret_plain  text,
    p_overlap_hours int DEFAULT 0
) RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = workflow, public, pg_catalog
AS $$
DECLARE
    enc_key text := current_setting('app.audit_encryption_key', true);
    n_existing int;
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
    IF p_overlap_hours < 0 THEN
        RAISE EXCEPTION 'overlap_hours must be >= 0, got %', p_overlap_hours;
    END IF;

    -- Retire any currently-active kids whose valid_until is open.
    -- With overlap=0 we cut them off NOW; with overlap>0 we extend
    -- their window so both old + new verify during the overlap.
    UPDATE workflow.flow_jwt_keys
       SET valid_until = clock_timestamp() + make_interval(hours => p_overlap_hours)
     WHERE flow_name = p_flow_name
       AND valid_until IS NULL;

    -- If this kid already exists on this flow, re-activate it (lift
    -- valid_until to NULL, refresh ciphertext). Otherwise insert.
    INSERT INTO workflow.flow_jwt_keys (flow_name, kid, ciphertext, valid_from, valid_until)
    VALUES (
        p_flow_name,
        p_secret_kid,
        pgp_sym_encrypt(p_secret_plain, enc_key)::bytea,
        clock_timestamp(),
        NULL
    )
    ON CONFLICT (flow_name, kid)
    DO UPDATE SET
        ciphertext = EXCLUDED.ciphertext,
        valid_from = EXCLUDED.valid_from,
        valid_until = NULL;

    -- Keep the Phase 5 Step 2 denormalised columns in sync — the
    -- Python loader still reads them via get_flow_jwt_secret() for
    -- the mint path. Verify uses the new keys table directly.
    UPDATE workflow.flow_registry
       SET jwt_secret_kid        = p_secret_kid,
           jwt_secret_ciphertext = pgp_sym_encrypt(p_secret_plain, enc_key)::bytea,
           updated_at            = clock_timestamp()
     WHERE flow_name = p_flow_name;

    GET DIAGNOSTICS n_existing = ROW_COUNT;
    IF n_existing = 0 THEN
        RAISE EXCEPTION 'flow_registry update unexpectedly affected 0 rows';
    END IF;
END;
$$;

GRANT EXECUTE ON FUNCTION
    workflow.set_flow_jwt_secret(text, text, text, int)
    TO georag_app;

-- ---------------------------------------------------------------------------
-- Getter (mint path) — returns the SINGLE row to sign new tokens with.
-- Picks the most recently activated kid that's currently within its
-- valid window.
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
        SELECT k.kid,
               pgp_sym_decrypt(k.ciphertext, enc_key)
          FROM workflow.flow_jwt_keys k
         WHERE k.flow_name = p_flow_name
           AND k.valid_from <= clock_timestamp()
           AND (k.valid_until IS NULL OR k.valid_until > clock_timestamp())
         ORDER BY k.valid_from DESC
         LIMIT 1;
END;
$$;

GRANT EXECUTE ON FUNCTION workflow.get_flow_jwt_secret(text) TO georag_app;

-- ---------------------------------------------------------------------------
-- Multi-kid getter (verify path) — returns every kid currently in its
-- valid window. The Python verifier matches the inbound `kid` claim
-- against any row in this set.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION workflow.get_flow_jwt_keys(p_flow_name text)
RETURNS TABLE (kid text, plain text, valid_until timestamptz)
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
        SELECT k.kid,
               pgp_sym_decrypt(k.ciphertext, enc_key),
               k.valid_until
          FROM workflow.flow_jwt_keys k
         WHERE k.flow_name = p_flow_name
           AND k.valid_from <= clock_timestamp()
           AND (k.valid_until IS NULL OR k.valid_until > clock_timestamp())
         ORDER BY k.valid_from DESC;
END;
$$;

GRANT EXECUTE ON FUNCTION workflow.get_flow_jwt_keys(text) TO georag_app;

DO $$
DECLARE
    n_tbl int;
    n_fn  int;
BEGIN
    SELECT count(*) INTO n_tbl FROM information_schema.tables
     WHERE table_schema='workflow' AND table_name='flow_jwt_keys';
    SELECT count(*) INTO n_fn FROM information_schema.routines
     WHERE routine_schema='workflow'
       AND routine_name IN ('set_flow_jwt_secret','get_flow_jwt_secret','get_flow_jwt_keys');
    RAISE NOTICE 'Phase 6 Step 3: tbl=% fns=%', n_tbl, n_fn;
    IF n_tbl <> 1 OR n_fn <> 3 THEN
        RAISE EXCEPTION 'Phase 6 Step 3 install incomplete';
    END IF;
END $$;
