-- =============================================================================
-- Phase 15 Step 1 — silver agent-prompt MV refresh helper (R-P14-2).
--
-- The agent's NUMERIC system-prompt path reads silver.mv_collar_summary
-- via orchestrator.py:_build_project_facts. If the MV is stale or empty,
-- the agent omits the HIGH-CONFIDENCE SUMMARIES block and the LLM
-- responds "I don't have that number in this project" (Phase 14 R-P13-1
-- root cause).
--
-- Dagster's ingestion pipeline is expected to refresh this MV after
-- every batch, but in dev / paused-Dagster environments it drifts.
-- The Phase 15 nightly Hatchet workflow mv_refresh_silver runs this
-- function at 03:00 UTC so the agent always has fresh facts.
--
-- The function is intentionally narrow — refreshes only the MVs the
-- agent's prompt-building path reads. Adding new agent-prompt MVs?
-- Add their REFRESH MATERIALIZED VIEW line here.
--
-- Idempotent.
-- =============================================================================

CREATE OR REPLACE FUNCTION workflow.refresh_silver_agent_mvs()
RETURNS TABLE (mv_name text, refreshed_at timestamptz)
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = workflow, silver, public, pg_catalog
AS $$
BEGIN
    -- silver.mv_collar_summary — agent's per-project facts source
    REFRESH MATERIALIZED VIEW silver.mv_collar_summary;
    mv_name := 'silver.mv_collar_summary';
    refreshed_at := clock_timestamp();
    RETURN NEXT;

    -- Future: additional agent-prompt MVs land here, one REFRESH +
    -- RETURN NEXT block each. CONCURRENTLY is preferable for any
    -- MV that has a UNIQUE index — silver.mv_collar_summary does
    -- not currently, so we use the simpler form.
END;
$$;

GRANT EXECUTE ON FUNCTION workflow.refresh_silver_agent_mvs() TO georag_app;

DO $$
DECLARE
    n_fn int;
BEGIN
    SELECT count(*) INTO n_fn FROM information_schema.routines
     WHERE routine_schema = 'workflow'
       AND routine_name = 'refresh_silver_agent_mvs';
    IF n_fn <> 1 THEN
        RAISE EXCEPTION 'Phase 15 Step 1 install incomplete: refresh fn missing';
    END IF;
END $$;
