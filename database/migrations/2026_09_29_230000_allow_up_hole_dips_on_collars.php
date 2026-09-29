<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;

/**
 * §04e: up-holes are allowed (SME-approved, Kyle, 2026-09-29).
 *
 * chk_dip_range (2026_04_13_100000_database_hardening.php:70-74) held
 * `dip >= -90 AND dip <= 0`, so an underground fan drilled above horizontal
 * could not be stored at all — the ingest guard blanked every positive dip
 * rather than let one row abort a 500-row batch. The convention is unchanged
 * (negative = below horizontal); the range now covers the upper hemisphere:
 * `-90 <= dip <= 90`.
 *
 * silver.surveys carries no dip CHECK (2026_04_09_180200_create_surveys_table
 * never had one, and no later migration added one), so there is nothing to
 * widen there; no other drill-hole table carries a dip CHECK. The structure
 * tables' `dip BETWEEN 0 AND 90` is the dip of a PLANE, not of a hole, and is
 * deliberately untouched.
 *
 * Widening a CHECK can never reject an existing row, so the validating scan
 * in up() is safe on a populated table. down() narrows it again; if up-holes
 * have been stored by then, the narrow constraint is added NOT VALID — new
 * rows are held to -90..0, the stored up-holes are kept rather than deleted
 * — and a warning names the count.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('ALTER TABLE silver.collars DROP CONSTRAINT IF EXISTS chk_dip_range');
        DB::statement('ALTER TABLE silver.collars ADD CONSTRAINT chk_dip_range CHECK (dip >= -90 AND dip <= 90)');
        DB::statement(<<<'SQL'
            COMMENT ON CONSTRAINT chk_dip_range ON silver.collars IS
            'Dip from horizontal, negative = below horizontal. -90..90: up-holes (dip > 0) are stored as measured (§04e, SME-approved 2026-09-29).'
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        $upHoles = (int) DB::scalar('SELECT count(*) FROM silver.collars WHERE dip > 0');

        // Built from a variable so the migration-mirror test
        // (src/fastapi/tests/test_silver_row_guard.py) reads the up() CHECK,
        // which is the one in force after migrating.
        $narrow = 'CHECK (dip >= -90 AND dip <= 0)';

        DB::statement('ALTER TABLE silver.collars DROP CONSTRAINT IF EXISTS chk_dip_range');

        if ($upHoles === 0) {
            DB::statement("ALTER TABLE silver.collars ADD CONSTRAINT chk_dip_range {$narrow}");

            return;
        }

        DB::statement("ALTER TABLE silver.collars ADD CONSTRAINT chk_dip_range {$narrow} NOT VALID");
        Log::warning('chk_dip_range narrowed to -90..0 NOT VALID: stored up-holes kept', [
            'up_hole_collars' => $upHoles,
        ]);
    }
};
