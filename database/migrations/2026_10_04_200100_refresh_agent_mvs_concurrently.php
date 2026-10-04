<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * workflow.refresh_silver_agent_mvs(): REFRESH ... CONCURRENTLY
 * (database audit 2026-10).
 *
 * 2026_08_19_040000 installed this function with a plain
 * `REFRESH MATERIALIZED VIEW silver.mv_collar_summary`, which takes an
 * ACCESS EXCLUSIVE lock for the whole rebuild. mv_refresh_silver.py calls it
 * every night, and every chat query's _build_project_facts reads that view,
 * so each of them stalled for as long as the aggregate over silver.collars /
 * samples / lithology_logs took.
 *
 * The view has had a unique index (idx_mv_collar_summary_project, project_id,
 * recreated by 2026_09_29_210300) since the fan-out rebuild, which is what
 * CONCURRENTLY requires; services/mv_refresh.py has always refreshed it that
 * way. Readers now keep seeing the previous contents until the new ones are
 * swapped in.
 *
 * CONCURRENTLY cannot populate a view built WITH NO DATA, so that one case
 * (ispopulated = false) still takes the plain form -- a first fill has no
 * readers to protect, and without the branch the function would error on it.
 *
 * Also in this replacement, because CREATE OR REPLACE rewrites proconfig
 * anyway: search_path is `pg_catalog, workflow` instead of `workflow, silver,
 * public, pg_catalog`. This is SECURITY DEFINER; with `public` ahead of
 * pg_catalog a user who can create objects in public could shadow a built-in
 * the body calls. Everything the body names is schema-qualified, so nothing
 * needs the old path. (EXECUTE is revoked from PUBLIC by
 * 2026_10_04_200500.)
 *
 * Signature, return shape, owner and existing grants are unchanged
 * (CREATE OR REPLACE keeps the ACL), so mv_refresh_silver.py is untouched.
 * REFRESH requires owning the view (or MAINTAIN on PG 17+): the owner is the
 * migration role, as it is for the function.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (! DB::selectOne("SELECT 1 AS present FROM information_schema.schemata WHERE schema_name = 'workflow'")) {
            return;
        }

        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION workflow.refresh_silver_agent_mvs()
            RETURNS TABLE (mv_name text, refreshed_at timestamptz)
                LANGUAGE plpgsql
                SECURITY DEFINER
                SET search_path = pg_catalog, workflow
            AS $fn$
            DECLARE
                v_populated boolean;
            BEGIN
                SELECT m.ispopulated INTO v_populated
                  FROM pg_catalog.pg_matviews m
                 WHERE m.schemaname = 'silver'
                   AND m.matviewname = 'mv_collar_summary';

                IF v_populated THEN
                    REFRESH MATERIALIZED VIEW CONCURRENTLY silver.mv_collar_summary;
                ELSE
                    REFRESH MATERIALIZED VIEW silver.mv_collar_summary;
                END IF;

                mv_name := 'silver.mv_collar_summary';
                refreshed_at := clock_timestamp();
                RETURN NEXT;
            END;
            $fn$;
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (! DB::selectOne("SELECT 1 AS present FROM information_schema.schemata WHERE schema_name = 'workflow'")) {
            return;
        }

        // 2026_08_19_040000, verbatim.
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
    }
};
