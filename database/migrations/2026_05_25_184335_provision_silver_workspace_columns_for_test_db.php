<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Test-DB parity migration — adds the `workspace_id` UUID column that
 * `database/raw/phase0/96-rls-tenant-isolation-block1.sql` installs on
 * the four silver tables below in production, but which never made it
 * into the Laravel migration chain. Same pattern as
 * 2026_05_24_220000_provision_reports_workspace_columns_for_test_db.
 *
 * Without this migration:
 *
 *   - tests/Feature/Tenancy/BronzeProvenanceAutofillTriggerTest skips
 *     the two trigger-autofill cases that need silver.collars.
 *   - tests/Feature/Foundry/LakehouseAndDrillholeDetailTest branches
 *     its seedCollar helper depending on whether workspace_id exists.
 *
 * Tables covered (silver) — each one is referenced by the autofill
 * trigger added in 2026_05_25_175601:
 *
 *   - silver.collars
 *   - silver.lithology_logs
 *   - silver.raster_layers
 *   - silver.samples
 *
 * Three columns already had workspace_id in the test DB
 * (silver.reports, silver.spatial_features, silver.assays_v2,
 * silver.geophysics_surveys) — they were either created by a Laravel
 * migration that included the column, or covered by 2026_05_24_220000.
 *
 * **Idempotent — ADD COLUMN IF NOT EXISTS keeps production a strict
 * no-op since prod already has the column from phase0 raw SQL.**
 *
 * SQLite (test DB used for the other tests) doesn't have these silver
 * tables either; the migration is gated on Postgres.
 */
return new class extends Migration
{
    /** @var list<string> */
    private const TABLES = [
        'collars',
        'lithology_logs',
        'raster_layers',
        'samples',
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        foreach (self::TABLES as $tbl) {
            $exists = DB::table('information_schema.tables')
                ->where('table_schema', 'silver')
                ->where('table_name', $tbl)
                ->exists();
            if (! $exists) {
                continue;
            }
            DB::statement("ALTER TABLE silver.{$tbl} ADD COLUMN IF NOT EXISTS workspace_id uuid");
        }
    }

    public function down(): void
    {
        // Intentionally no-op. Rolling back would drop the column in
        // both production (where it's load-bearing for RLS) and test
        // DB. If you genuinely need to drop it, do so via a forward
        // migration with a clear safety review.
    }
};
