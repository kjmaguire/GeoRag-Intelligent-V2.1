-- =============================================================================
-- Phase 7 Step 2 — auto-prune expired flow_jwt_keys rows (R-P6-2).
--
-- Phase 6 Step 3 added per-flow JWT key history with optional
-- valid_until windows. Nothing reaps expired rows today, so the table
-- accumulates dead kids forever. This migration adds the SECURITY
-- DEFINER reaper function that the Hatchet `flow_jwt_key_reaper`
-- workflow calls nightly.
--
-- Retention policy: keep a row for `retention_days` (default 7) past
-- its valid_until. After that the row is gone — verifies for tokens
-- signed with that kid will reject as "kid not in registry", which is
-- the intended end state of a rotation.
--
-- Idempotent.
-- =============================================================================

CREATE OR REPLACE FUNCTION workflow.reap_expired_flow_jwt_keys(
    p_retention_days int DEFAULT 7
) RETURNS TABLE (deleted_count int, oldest_kept timestamptz)
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = workflow, public, pg_catalog
AS $$
DECLARE
    n_deleted int;
BEGIN
    IF p_retention_days < 0 THEN
        RAISE EXCEPTION 'retention_days must be >= 0, got %', p_retention_days;
    END IF;

    WITH culled AS (
        DELETE FROM workflow.flow_jwt_keys
         WHERE valid_until IS NOT NULL
           AND valid_until < clock_timestamp() - make_interval(days => p_retention_days)
         RETURNING 1
    )
    SELECT count(*)::int INTO n_deleted FROM culled;

    RETURN QUERY
        SELECT n_deleted,
               (SELECT min(valid_until)
                  FROM workflow.flow_jwt_keys
                 WHERE valid_until IS NOT NULL);
END;
$$;

GRANT EXECUTE ON FUNCTION workflow.reap_expired_flow_jwt_keys(int) TO georag_app;

DO $$
DECLARE
    n_fn int;
BEGIN
    SELECT count(*) INTO n_fn FROM information_schema.routines
     WHERE routine_schema='workflow' AND routine_name='reap_expired_flow_jwt_keys';
    IF n_fn <> 1 THEN
        RAISE EXCEPTION 'Phase 7 Step 2 install incomplete: reap function missing';
    END IF;
END $$;
