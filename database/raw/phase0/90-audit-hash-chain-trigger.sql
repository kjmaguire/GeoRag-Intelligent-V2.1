-- =============================================================================
-- Phase 0 — audit_ledger hash-chain trigger
--
-- BEFORE-INSERT trigger that:
--   1. Looks up the previous row's hash (scoped per workspace; global chain
--      for system-wide events with workspace_id IS NULL) -- two indexable
--      branches, not IS NOT DISTINCT FROM (2026_10_04_200000).
--   2. Locks that row FOR UPDATE (after a per-workspace advisory lock) so
--      concurrent inserts can't collide.
--   3. Computes this row's hash from previous_hash + canonical content.
--
-- The verification job (Step 4 — audit_ledger_verify Hatchet workflow) walks
-- the chain by re-running this exact computation against stored fields and
-- comparing to the stored hash.
--
-- Hash recipe (also documented in docs/audit_ledger_hash_recipe.md):
--   sha256( hex(previous_hash) || '|' || actor_id || '|' || actor_kind
--           || '|' || action_type || '|' || target_schema || '|' ||
--           target_table || '|' || target_id || '|' || payload_canonical
--           || '|' || created_at_iso_utc )
--
-- payload_canonical is the postgres jsonb::text serialisation, which is
-- deterministic for a given jsonb value (length-then-lex key order).
-- =============================================================================

-- pgcrypto provides digest() / SHA-256. Pin to the public schema explicitly:
-- the database-level search_path is set to `silver, bronze, gold, index,
-- public` (see init-postgis.sql), so a bare CREATE EXTENSION pgcrypto would
-- land it in silver — fine for georag-postgresql sessions but invisible to
-- PgBouncer-pooled Laravel sessions whose search_path differs. Always-public
-- removes the variability; the trigger schema-qualifies as public.digest().
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;

CREATE OR REPLACE FUNCTION audit.compute_audit_hash() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_prev_hash bytea;
    v_message   text;
BEGIN
    -- Serialise concurrent inserts to the same workspace's chain.
    -- The 2026-05-16 report-build burst proved that row-level
    -- FOR UPDATE alone does not fence concurrent writers under
    -- partition-parent contention. The advisory lock is keyed on
    -- workspace_id so cross-workspace traffic stays parallel.
    -- (Mirrors migrations 2026_05_19_180300 and 2026_10_04_200000 so a raw
    -- apply cannot revert either.)
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            'audit_chain_'
            || COALESCE(NEW.workspace_id::text, 'system'),
            0
        )
    );

    -- Now safe to read the latest row: any concurrent writer
    -- in this workspace is blocked behind us on the lock above.
    -- The FOR UPDATE here is belt-and-braces; the advisory lock
    -- is the load-bearing serialiser.
    --
    -- Two branches rather than `workspace_id IS NOT DISTINCT FROM
    -- NEW.workspace_id`: that operator cannot use an index, and
    -- this ran as a sequential scan + sort on every insert.
    -- Each branch is an index probe on
    -- audit_ledger_workspace_id_idx (workspace_id, created_at DESC).
    IF NEW.workspace_id IS NULL THEN
        -- `workspace_id` leads the ORDER BY on purpose. For an
        -- `= value` qual the planner knows the column is constant
        -- and drops it from the sort; for `IS NULL` it does not,
        -- so without it the index (workspace_id, created_at DESC)
        -- is not seen as presorted and the planner falls back to
        -- the very scan + sort this migration removes (measured:
        -- 56 ms vs 0.25 ms at 300k rows). Every row here has a
        -- NULL workspace_id, so the extra key orders nothing and
        -- the row chosen is the same one.
        SELECT hash INTO v_prev_hash
        FROM audit.audit_ledger
        WHERE workspace_id IS NULL
        ORDER BY workspace_id, created_at DESC, id DESC
        LIMIT 1
        FOR UPDATE;
    ELSE
        SELECT hash INTO v_prev_hash
        FROM audit.audit_ledger
        WHERE workspace_id = NEW.workspace_id
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        FOR UPDATE;
    END IF;

    NEW.previous_hash := v_prev_hash;

    v_message := COALESCE(encode(v_prev_hash, 'hex'), '')
              || '|' || COALESCE(NEW.actor_id::text, '')
              || '|' || COALESCE(NEW.actor_kind, '')
              || '|' || NEW.action_type
              || '|' || COALESCE(NEW.target_schema, '')
              || '|' || COALESCE(NEW.target_table, '')
              || '|' || COALESCE(NEW.target_id, '')
              || '|' || NEW.payload::text
              || '|' || to_char(NEW.created_at AT TIME ZONE 'UTC',
                                'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');

    -- Schema-qualify digest() so it resolves even when the calling session's
    -- search_path excludes public (e.g. PgBouncer-pooled Laravel).
    NEW.hash := public.digest(v_message, 'sha256');
    RETURN NEW;
END $$;

COMMENT ON FUNCTION audit.compute_audit_hash() IS
    'BEFORE-INSERT trigger: computes audit_ledger.hash via SHA-256 over previous_hash + canonical content. See docs/audit_ledger_hash_recipe.md.';

-- Drop + recreate so re-running the script picks up function changes.
DROP TRIGGER IF EXISTS audit_ledger_compute_hash_trg ON audit.audit_ledger;
CREATE TRIGGER audit_ledger_compute_hash_trg
    BEFORE INSERT ON audit.audit_ledger
    FOR EACH ROW
    EXECUTE FUNCTION audit.compute_audit_hash();

-- Documenting the recipe in the database itself for the verification job.
INSERT INTO audit.audit_ledger
    (workspace_id, actor_id, actor_kind, action_type, target_schema, target_table, target_id, payload)
SELECT
    NULL, NULL, 'system', 'audit_ledger.genesis', 'audit', 'audit_ledger', NULL,
    jsonb_build_object(
        'phase', 'phase0_step2',
        'recipe', 'sha256(hex(previous_hash)|||actor_id||...||payload_text||created_at_iso_utc)',
        'note', 'genesis row — previous_hash is NULL'
    )
WHERE NOT EXISTS (
    SELECT 1 FROM audit.audit_ledger WHERE action_type = 'audit_ledger.genesis'
);
