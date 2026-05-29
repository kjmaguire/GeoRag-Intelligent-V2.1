<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Sprint 2 parser hardening — add hole_id_canonical for fuzzy join reliability.
 *
 * Hole IDs arrive in many formats: LEB-23-001, leb_23_001, LEB 23 001, etc.
 * The new hole_id_canonical column stores a normalized form (LEB23001) for reliable
 * cross-sample joins and foreign-key lookups. The display form (hole_id) is unchanged.
 * Nullable during rollout; parsers populate on next ingest. Historical backfill is
 * handled separately (see docs/RUNBOOK.md for the canonicalize:collars Artisan command).
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('ALTER TABLE silver.collars ADD COLUMN IF NOT EXISTS hole_id_canonical VARCHAR(50) NULL');

        // Unique constraint per project: same project + same canonical form = collision.
        // Partial unique index allows historical NULLs to coexist.
        DB::statement("
            CREATE UNIQUE INDEX IF NOT EXISTS uq_collars_project_hole_canonical
                ON silver.collars (project_id, hole_id_canonical)
                WHERE hole_id_canonical IS NOT NULL
        ");

        // Btree index for canonical lookups.
        DB::statement("
            CREATE INDEX IF NOT EXISTS idx_collars_hole_canonical
                ON silver.collars (hole_id_canonical)
        ");
    }

    public function down(): void
    {
        DB::statement('DROP INDEX IF EXISTS silver.uq_collars_project_hole_canonical');
        DB::statement('DROP INDEX IF EXISTS silver.idx_collars_hole_canonical');
        DB::statement('ALTER TABLE silver.collars DROP COLUMN IF EXISTS hole_id_canonical');
    }
};
