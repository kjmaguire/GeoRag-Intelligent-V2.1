<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * CC-01 Item 2 — Spatial uncertainty + CRS provenance on silver.collars and
 * silver.spatial_features.
 *
 * Adds the three fields the CC-01 verification flagged as missing for the
 * map-uncertainty surfacing:
 *
 *   spatial_uncertainty_m  real       — radius of positional uncertainty in
 *                                       metres around the point/feature
 *   crs_confidence         real (0-1) — confidence the recorded CRS is
 *                                       correct ('detected' = high,
 *                                       'declared' = highest,
 *                                       'assumed' = low, NULL = unknown)
 *   georef_method          varchar(16) CHECK enum:
 *                            'declared'  — CRS stated explicitly in source metadata
 *                            'detected'  — CRS inferred from the spatial pipeline
 *                                          (coordinate-range / projected-extent fit)
 *                            'assumed'   — fallback projection applied (e.g. UTM
 *                                          zone derived from project bbox)
 *                            'manual'    — geologist set the value in the UI
 *                            'survey'    — exact survey instrument datum
 *
 * Additive only — all columns nullable. Existing rows are unaffected.
 *
 * silver.raster_layers already carries crs_confidence (migration
 * 2026_04_18_140000); this migration extends the same field semantics to
 * vector collars + spatial features so the MapView ring renderer can pull
 * uniformly across all three.
 *
 * SQLite — gated on Postgres.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        foreach (['silver.collars', 'silver.spatial_features'] as $table) {
            DB::statement(<<<SQL
                ALTER TABLE {$table}
                  ADD COLUMN IF NOT EXISTS spatial_uncertainty_m  real,
                  ADD COLUMN IF NOT EXISTS crs_confidence         real,
                  ADD COLUMN IF NOT EXISTS georef_method          varchar(16)
            SQL);

            // Drop-and-replace makes the migration rerunnable. NOT VALID
            // skips the historic-row validation so adding the constraint
            // is fast on tables with millions of rows.
            $bareName = explode('.', $table)[1];
            DB::statement(<<<SQL
                ALTER TABLE {$table}
                  DROP CONSTRAINT IF EXISTS chk_{$bareName}_crs_confidence
            SQL);
            DB::statement(<<<SQL
                ALTER TABLE {$table}
                  ADD CONSTRAINT chk_{$bareName}_crs_confidence
                  CHECK (crs_confidence IS NULL
                         OR (crs_confidence >= 0 AND crs_confidence <= 1))
                  NOT VALID
            SQL);

            DB::statement(<<<SQL
                ALTER TABLE {$table}
                  DROP CONSTRAINT IF EXISTS chk_{$bareName}_georef_method
            SQL);
            DB::statement(<<<SQL
                ALTER TABLE {$table}
                  ADD CONSTRAINT chk_{$bareName}_georef_method
                  CHECK (georef_method IS NULL
                         OR georef_method IN ('declared', 'detected', 'assumed', 'manual', 'survey'))
                  NOT VALID
            SQL);

            DB::statement(<<<SQL
                ALTER TABLE {$table}
                  DROP CONSTRAINT IF EXISTS chk_{$bareName}_uncertainty_nonneg
            SQL);
            DB::statement(<<<SQL
                ALTER TABLE {$table}
                  ADD CONSTRAINT chk_{$bareName}_uncertainty_nonneg
                  CHECK (spatial_uncertainty_m IS NULL OR spatial_uncertainty_m >= 0)
                  NOT VALID
            SQL);

            DB::statement("COMMENT ON COLUMN {$table}.spatial_uncertainty_m IS 'CC-01 Item 2 — radius of positional uncertainty in metres. NULL = not recorded; UI map ring is omitted.'");
            DB::statement("COMMENT ON COLUMN {$table}.crs_confidence IS 'CC-01 Item 2 — confidence (0-1) that the recorded CRS is correct. NULL = unknown.'");
            DB::statement("COMMENT ON COLUMN {$table}.georef_method IS 'CC-01 Item 2 — how the spatial location was assigned. See chk_*_georef_method for the vocabulary.'");
        }

        // Partial indexes — filtering by georef_method = 'assumed' is a
        // common QA query path (find every collar whose CRS we guessed).
        DB::statement("CREATE INDEX IF NOT EXISTS idx_collars_assumed_crs ON silver.collars (georef_method) WHERE georef_method = 'assumed'");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_spatial_features_assumed_crs ON silver.spatial_features (georef_method) WHERE georef_method = 'assumed'");
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement('DROP INDEX IF EXISTS silver.idx_collars_assumed_crs');
        DB::statement('DROP INDEX IF EXISTS silver.idx_spatial_features_assumed_crs');

        foreach (['silver.collars', 'silver.spatial_features'] as $table) {
            $bareName = explode('.', $table)[1];
            DB::statement("ALTER TABLE {$table} DROP CONSTRAINT IF EXISTS chk_{$bareName}_crs_confidence");
            DB::statement("ALTER TABLE {$table} DROP CONSTRAINT IF EXISTS chk_{$bareName}_georef_method");
            DB::statement("ALTER TABLE {$table} DROP CONSTRAINT IF EXISTS chk_{$bareName}_uncertainty_nonneg");
            DB::statement(<<<SQL
                ALTER TABLE {$table}
                  DROP COLUMN IF EXISTS georef_method,
                  DROP COLUMN IF EXISTS crs_confidence,
                  DROP COLUMN IF EXISTS spatial_uncertainty_m
            SQL);
        }
    }
};
