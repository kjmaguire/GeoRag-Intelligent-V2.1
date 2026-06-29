<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * CC-01 Item 2 — spatial_uncertainty_m backfill + auto-derivation.
 *
 * Kyle approved (2026-06-29) the STANDARD survey-class rubric:
 *   DGPS/RTK survey   ±0.5 m   detected/manual digitised  ±25 m
 *   declared in report ±5 m    assumed / legacy           ±50 m
 *
 * Mapped onto the existing georef_method vocabulary
 * ({survey, declared, detected, manual, assumed}) and the documented
 * spatial_uncertainty_method rule names. Implemented as a BEFORE trigger so
 * EVERY future collar gets a defensible uncertainty with zero ingester code
 * changes (same pattern as the bronze.provenance autofill trigger), PLUS a
 * one-time backfill for existing rows.
 *
 * Design guardrails:
 *   - Only DERIVES when spatial_uncertainty_m IS NULL — never clobbers a value
 *     set by a direct measurement (spatial_uncertainty_method stays NULL there).
 *   - georef_method NULL/unknown → leaves spatial_uncertainty_m NULL, which the
 *     map UI correctly renders as "no ring" (per the column comment). We do NOT
 *     invent a 50 m ring for genuinely-unrecorded locations.
 *
 * NOTE: silver.collars is empty at apply time, so the backfill is a no-op now;
 * the trigger makes the rubric load-bearing the moment collar data lands.
 */
return new class extends Migration
{
    public function getConnection(): ?string
    {
        return config('database.default') === 'sqlite' ? null : 'pgsql_migrations';
    }

    public function up(): void
    {
        if (config('database.default') === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.derive_collar_spatial_uncertainty()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                -- Derive ONLY when not already set (don't overwrite a direct
                -- measurement) and when we have a method to key off.
                IF NEW.spatial_uncertainty_m IS NULL AND NEW.georef_method IS NOT NULL THEN
                    CASE NEW.georef_method
                        WHEN 'survey' THEN
                            NEW.spatial_uncertainty_m := 0.5;
                            NEW.spatial_uncertainty_method := 'modern_ni43101_survey';
                        WHEN 'declared' THEN
                            NEW.spatial_uncertainty_m := 5;
                            NEW.spatial_uncertainty_method := 'modern_ni43101_declared';
                        WHEN 'detected' THEN
                            NEW.spatial_uncertainty_m := 25;
                            NEW.spatial_uncertainty_method := 'legacy_assumed_utm';
                        WHEN 'manual' THEN
                            NEW.spatial_uncertainty_m := 25;
                            NEW.spatial_uncertainty_method := 'hand_digitised';
                        WHEN 'assumed' THEN
                            NEW.spatial_uncertainty_m := 50;
                            NEW.spatial_uncertainty_method := 'legacy_assumed_utm';
                        ELSE
                            NULL; -- unknown method → leave NULL (UI omits ring)
                    END CASE;
                END IF;
                RETURN NEW;
            END
            $$;
        SQL);

        DB::statement('DROP TRIGGER IF EXISTS trg_derive_collar_spatial_uncertainty ON silver.collars');
        DB::statement(<<<'SQL'
            CREATE TRIGGER trg_derive_collar_spatial_uncertainty
                BEFORE INSERT OR UPDATE ON silver.collars
                FOR EACH ROW
                EXECUTE FUNCTION silver.derive_collar_spatial_uncertainty();
        SQL);

        // One-time backfill for existing rows (no-op on the empty table).
        DB::statement(<<<'SQL'
            UPDATE silver.collars
               SET spatial_uncertainty_m = CASE georef_method
                       WHEN 'survey'   THEN 0.5
                       WHEN 'declared' THEN 5
                       WHEN 'detected' THEN 25
                       WHEN 'manual'   THEN 25
                       WHEN 'assumed'  THEN 50
                   END,
                   spatial_uncertainty_method = CASE georef_method
                       WHEN 'survey'   THEN 'modern_ni43101_survey'
                       WHEN 'declared' THEN 'modern_ni43101_declared'
                       WHEN 'detected' THEN 'legacy_assumed_utm'
                       WHEN 'manual'   THEN 'hand_digitised'
                       WHEN 'assumed'  THEN 'legacy_assumed_utm'
                   END
             WHERE spatial_uncertainty_m IS NULL
               AND georef_method IS NOT NULL;
        SQL);
    }

    public function down(): void
    {
        if (config('database.default') === 'sqlite') {
            return;
        }
        DB::statement('DROP TRIGGER IF EXISTS trg_derive_collar_spatial_uncertainty ON silver.collars');
        DB::statement('DROP FUNCTION IF EXISTS silver.derive_collar_spatial_uncertainty()');
    }
};
