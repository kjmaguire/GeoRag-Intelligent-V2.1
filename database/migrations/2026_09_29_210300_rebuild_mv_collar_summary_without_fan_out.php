<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Rebuild silver.mv_collar_summary so its per-project numbers are right
 * (§04e, database audit 2026-09-29 PG-1).
 *
 * The definition in 2026_04_13_200000_production_hardening_final.php:47 was
 *
 *   FROM silver.collars c
 *   LEFT JOIN silver.samples s        ON s.collar_id = c.collar_id
 *   LEFT JOIN silver.lithology_logs l ON l.collar_id = c.collar_id
 *   GROUP BY c.project_id
 *
 * which yields n_samples x n_litho rows per collar before the GROUP BY.
 * count(c.collar_id), count(s.sample_id) and avg/min/max(total_depth) were
 * all computed over that product. Two holes (100 m, 300 m) with 10 samples
 * and 5 litho intervals on the first came out as total_collars=51,
 * avg_depth=103.9, total_samples=50 — truth is 2, 200.0, 10. The
 * orchestrator's _build_project_facts injects these as "HIGH-CONFIDENCE
 * SUMMARIES (quote verbatim)", so every hole-count / mean-depth / sample
 * count answer was confidently wrong. database/raw/phase18/15 fixed it in
 * raw SQL, which the AWS migrate task never applies.
 *
 * Here samples and lithology_logs are pre-aggregated per collar in their
 * own subqueries, so each collar contributes exactly one row. The column
 * names, order and types are unchanged (counts stay bigint), so readers and
 * the REFRESH ... CONCURRENTLY path are unaffected.
 *
 * The unique index is recreated under its original name so CONCURRENTLY
 * refreshes still work, and the grants from 2026_08_20_040000 are reissued
 * (a dropped relation takes its ACL with it): SELECT, plus MAINTAIN on
 * PostgreSQL 17+ where REFRESH is gated by that privilege.
 *
 * WITH DATA: the view is populated from the current tables as part of the
 * migration, so no separate refresh is needed after deploy.
 *
 * Tenant note: a materialized view cannot carry RLS. This one is keyed by
 * project_id only and its reader filters by project_id; that is unchanged.
 */
return new class extends Migration
{
    private const FIXED = <<<'SQL'
        CREATE MATERIALIZED VIEW silver.mv_collar_summary AS
        SELECT
            c.project_id,
            count(*)                                   AS total_collars,
            avg(c.total_depth)::numeric(10,1)          AS avg_depth,
            min(c.total_depth)::numeric(10,1)          AS min_depth,
            max(c.total_depth)::numeric(10,1)          AS max_depth,
            count(DISTINCT c.hole_type)                AS hole_type_count,
            min(c.drill_date)                          AS earliest_drill,
            max(c.drill_date)                          AS latest_drill,
            COALESCE(sum(s.n), 0)::bigint              AS total_samples,
            COALESCE(sum(l.n), 0)::bigint              AS total_litho_intervals
        FROM silver.collars c
        LEFT JOIN (
            SELECT collar_id, count(*) AS n
              FROM silver.samples
             GROUP BY collar_id
        ) s ON s.collar_id = c.collar_id
        LEFT JOIN (
            SELECT collar_id, count(*) AS n
              FROM silver.lithology_logs
             GROUP BY collar_id
        ) l ON l.collar_id = c.collar_id
        GROUP BY c.project_id
        WITH DATA
    SQL;

    /** The 2026_04_13_200000 definition, restored by down() verbatim. */
    private const PREVIOUS = <<<'SQL'
        CREATE MATERIALIZED VIEW silver.mv_collar_summary AS
        SELECT
            c.project_id,
            COUNT(c.collar_id) AS total_collars,
            AVG(c.total_depth)::numeric(10,1) AS avg_depth,
            MIN(c.total_depth)::numeric(10,1) AS min_depth,
            MAX(c.total_depth)::numeric(10,1) AS max_depth,
            COUNT(DISTINCT c.hole_type) AS hole_type_count,
            MIN(c.drill_date) AS earliest_drill,
            MAX(c.drill_date) AS latest_drill,
            COUNT(s.sample_id) AS total_samples,
            COUNT(DISTINCT l.log_id) AS total_litho_intervals
        FROM silver.collars c
        LEFT JOIN silver.samples s ON s.collar_id = c.collar_id
        LEFT JOIN silver.lithology_logs l ON l.collar_id = c.collar_id
        GROUP BY c.project_id
        WITH DATA
    SQL;

    public function up(): void
    {
        $this->rebuild(self::FIXED);
    }

    public function down(): void
    {
        $this->rebuild(self::PREVIOUS);
    }

    private function rebuild(string $definition): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // No CASCADE: nothing in the chain depends on this view, and if
        // something ever does, failing here is better than dropping it.
        DB::statement('DROP MATERIALIZED VIEW IF EXISTS silver.mv_collar_summary');
        DB::statement($definition);
        DB::statement(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_collar_summary_project
                 ON silver.mv_collar_summary (project_id)',
        );

        $this->grantToApp();
    }

    /**
     * Same grant 2026_08_20_040000 made, for the same reason: the refresh
     * helper runs as georag_app. MAINTAIN only exists on PostgreSQL 17+,
     * probed via server_version_num because a failed GRANT would abort the
     * migration's transaction.
     */
    private function grantToApp(): void
    {
        $hasRole = DB::selectOne("SELECT 1 AS present FROM pg_roles WHERE rolname = 'georag_app'");
        if ($hasRole === null) {
            return;
        }

        $version = (int) DB::selectOne("SELECT current_setting('server_version_num')::int AS v")->v;
        $privileges = $version >= 170000 ? 'SELECT, MAINTAIN' : 'SELECT';

        DB::statement("GRANT {$privileges} ON TABLE silver.mv_collar_summary TO georag_app");
    }
};
