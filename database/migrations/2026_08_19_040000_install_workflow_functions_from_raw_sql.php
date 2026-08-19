<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Brings two `workflow` schema functions under version control.
 *
 * Both were only ever defined in `database/raw/` — phase7/10-flow-jwt-key-reaper.sql
 * and phase15/10-mv-refresh-fn.sql — which is applied by hand, not by the
 * migration chain. They therefore never reached the Azure server, and the two
 * Hatchet workflows that call them have failed on every scheduled run:
 *
 *   flow_jwt_key_reaper  04:00 UTC  function workflow.reap_expired_flow_jwt_keys(unknown) does not exist
 *   mv_refresh_silver    03:00 UTC  function workflow.refresh_silver_agent_mvs() does not exist
 *
 * The mv_refresh one is not cosmetic. It refreshes silver.mv_collar_summary,
 * which orchestrator.py:_build_project_facts reads to build the agent's
 * HIGH-CONFIDENCE SUMMARIES block. When that MV is stale the agent drops the
 * block and answers "I don't have that number in this project" — the documented
 * Phase 14 R-P13-1 root cause. So a silent nightly failure here degrades answer
 * quality without any user-visible error.
 *
 * Same class of defect as silver.collars.geom_4326 (migration 2026_08_19_010000):
 * schema applied out of band to dev, never declared, silently absent everywhere
 * else. The bodies below are copied verbatim from those raw files; if you edit
 * one, edit both or delete the raw file.
 *
 * Idempotent — CREATE OR REPLACE, and the GRANT is skipped when the role is
 * absent (local compose provisions roles differently from Azure).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        $hasWorkflowSchema = DB::selectOne(
            "SELECT 1 AS present FROM information_schema.schemata WHERE schema_name = 'workflow'",
        );

        if (! $hasWorkflowSchema) {
            // Nothing to attach to. The schema is created earlier in the chain;
            // if it is missing the install is broken in a way this migration
            // must not paper over.
            return;
        }

        // Phase 15 — agent-prompt MV refresh. plpgsql bodies are not parsed for
        // object references at CREATE time, so this succeeds even if the MV is
        // built later in the chain.
        DB::unprepared(<<<'SQL'
CREATE OR REPLACE FUNCTION workflow.refresh_silver_agent_mvs()
RETURNS TABLE (mv_name text, refreshed_at timestamptz)
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = workflow, silver, public, pg_catalog
AS $fn$
BEGIN
    REFRESH MATERIALIZED VIEW silver.mv_collar_summary;
    mv_name := 'silver.mv_collar_summary';
    refreshed_at := clock_timestamp();
    RETURN NEXT;
END;
$fn$;
SQL);

        // Phase 7 — expired flow JWT key reaper.
        DB::unprepared(<<<'SQL'
CREATE OR REPLACE FUNCTION workflow.reap_expired_flow_jwt_keys(
    p_retention_days int DEFAULT 7
) RETURNS TABLE (deleted_count int, oldest_kept timestamptz)
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = workflow, public, pg_catalog
AS $fn$
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
$fn$;
SQL);

        // The raw files grant EXECUTE to georag_app. Skipped when the role does
        // not exist so a fresh local cluster still migrates cleanly.
        $hasAppRole = DB::selectOne(
            "SELECT 1 AS present FROM pg_roles WHERE rolname = 'georag_app'",
        );

        if ($hasAppRole) {
            DB::unprepared(
                'GRANT EXECUTE ON FUNCTION workflow.refresh_silver_agent_mvs() TO georag_app;'
                .' GRANT EXECUTE ON FUNCTION workflow.reap_expired_flow_jwt_keys(int) TO georag_app;',
            );
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::unprepared('DROP FUNCTION IF EXISTS workflow.refresh_silver_agent_mvs();');
        DB::unprepared('DROP FUNCTION IF EXISTS workflow.reap_expired_flow_jwt_keys(int);');
    }
};
