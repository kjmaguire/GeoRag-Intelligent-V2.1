<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Drillhole schema — enable RLS + tenant-isolation policy on every
 * new silver/gold table from the 2026-05-20 drillhole stack.
 *
 * Policy shape matches the rest of the codebase (per
 * `2026_05_19_180100_enable_rls_on_uncovered_workspace_tables.php`):
 *
 *   USING (
 *     NULLIF(current_setting('app.workspace_id', true), '') IS NULL
 *     OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
 *   )
 *
 * The NULLIF/IS-NULL branch is the admin-escape-hatch — when no GUC
 * is set (i.e. internal admin tooling running directly as `georag`),
 * the policy permits full reads. Production traffic always goes
 * through the FastAPI request lifecycle which sets the GUC per
 * transaction, so RLS is effective there.
 *
 * Bronze tables are intentionally excluded — those are written/read
 * only by internal pipeline workers (Dagster bronze→silver assets)
 * and never by tenant-scoped queries.
 *
 * silver.element_reference is intentionally excluded — global
 * geochemistry reference data, no workspace_id column.
 *
 * SQLite (test DB) — gated on Postgres.
 */
return new class extends Migration
{
    /**
     * Tables to gate with the standard tenant-isolation policy.
     */
    private const TENANT_TABLES = [
        // silver
        'silver.campaigns',
        'silver.assays_v2',
        'silver.lithology',
        'silver.structure',
        'silver.alteration',
        'silver.mineralization',
        'silver.recovery',
        'silver.specific_gravity',
        'silver.geotechnical',
        'silver.downhole_geophysics',
        'silver.sample_intervals',
        'silver.sample_dispatches',
        'silver.qaqc_results',
        'silver.rock_codes',
        // gold
        'gold.assay_composites',
        'gold.significant_intersections',
        'gold.drill_summaries',
        'gold.zone_statistics',
        'gold.qaqc_statistics',
        'gold.campaign_summaries',
        'gold.element_correlations',
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        foreach (self::TENANT_TABLES as $qualified) {
            // Derive policy name from the table name (last segment).
            [$schema, $table] = explode('.', $qualified, 2);
            $policy = "{$table}_tenant_isolation";

            DB::statement("ALTER TABLE {$qualified} ENABLE ROW LEVEL SECURITY");
            DB::statement("DROP POLICY IF EXISTS {$policy} ON {$qualified}");
            DB::statement(<<<SQL
                CREATE POLICY {$policy} ON {$qualified}
                  USING (
                    NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                    OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                  )
            SQL);
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        foreach (self::TENANT_TABLES as $qualified) {
            [$schema, $table] = explode('.', $qualified, 2);
            $policy = "{$table}_tenant_isolation";
            DB::statement("DROP POLICY IF EXISTS {$policy} ON {$qualified}");
            DB::statement("ALTER TABLE {$qualified} DISABLE ROW LEVEL SECURITY");
        }
    }
};
