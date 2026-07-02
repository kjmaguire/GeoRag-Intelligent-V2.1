<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Audit remediation 2026-07-02 — reconcile collar spatial uncertainty to the
 * Kyle-approved Strategy B rubric (2026-05-24).
 *
 * The original revision of 2026_06_29_010000_derive_collar_spatial_uncertainty
 * ran on the live cluster with an INVENTED value set (0.5/5/25/50 m) under a
 * false "Kyle approved (2026-06-29)" comment, and with method labels that
 * contradicted Strategy B (e.g. 'modern_ni43101_survey' at ±0.5 m vs the
 * approved ±7 m tier; 'legacy_assumed_utm' at 25/50 m vs the approved 175 m).
 * That file has been corrected in place, but clusters where it already ran
 * (the migrations table records it) never re-execute it — so this follow-up:
 *
 *   1. CREATE OR REPLACEs the trigger function with the Strategy B rules
 *      (idempotent; identical body to the corrected 2026_06_29 migration).
 *   2. Recreates the trigger (no-op change, kept for drift safety).
 *   3. Reconciles any rows written by the invented-value trigger, matched by
 *      their exact (value, method) signatures — none of which Strategy B can
 *      produce — by re-deriving them under Strategy B.
 *   4. Re-runs the standard Strategy B backfill for still-NULL rows.
 *
 * At authoring time silver.collars was EMPTY on the live cluster (verified
 * 2026-07-02), so steps 3-4 are expected no-ops there; they exist so any
 * cluster that ingested collars between the two migrations is still healed.
 *
 * Invented-signature table (old trigger + old backfill):
 *   survey   → 0.5 m 'modern_ni43101_survey'
 *   declared → 5 m   'modern_ni43101_declared'
 *   detected → 25 m  'legacy_assumed_utm'
 *   manual   → 25 m  'hand_digitised'
 *   assumed  → 50 m  'legacy_assumed_utm'
 * Strategy B never emits any of these (value, method) pairs, so matching on
 * the pair cannot touch a legitimately-measured or Strategy-B-derived row.
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

        // Idempotent on clusters that already have the column (live) and
        // creates it on any cluster that somehow lacks it.
        DB::statement(<<<'SQL'
            ALTER TABLE silver.collars
                ADD COLUMN IF NOT EXISTS spatial_uncertainty_method VARCHAR(100)
        SQL);

        // 1. Replace the trigger function with the Strategy B rules.
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.derive_collar_spatial_uncertainty()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                is_modern boolean;
            BEGIN
                IF NEW.spatial_uncertainty_m IS NULL AND NEW.georef_method IS NOT NULL THEN
                    -- Strategy B era flag: pre/post-2010 on COALESCE(drill_date,
                    -- created_at). NULL (both dates missing) is conservatively
                    -- legacy — `is_modern IS TRUE` below treats NULL as false.
                    is_modern := COALESCE(NEW.drill_date::date, NEW.created_at::date)
                                     >= DATE '2010-01-01';

                    IF NEW.georef_method = 'survey' THEN
                        NEW.spatial_uncertainty_m := 10.0;
                        NEW.spatial_uncertainty_method := 'government_gps';
                    ELSIF NEW.georef_method IN ('declared', 'detected', 'manual') THEN
                        IF is_modern IS TRUE THEN
                            NEW.spatial_uncertainty_m := 35.0;
                            NEW.spatial_uncertainty_method := 'modern_ni43101_declared';
                        ELSE
                            NEW.spatial_uncertainty_m := 75.0;
                            NEW.spatial_uncertainty_method := 'legacy_declared';
                        END IF;
                    ELSIF NEW.georef_method = 'assumed' THEN
                        NEW.spatial_uncertainty_m := 175.0;
                        NEW.spatial_uncertainty_method := 'legacy_assumed_utm';
                    END IF;
                END IF;
                RETURN NEW;
            END
            $$;
        SQL);

        // 2. Recreate the trigger (same definition; drift safety).
        DB::statement('DROP TRIGGER IF EXISTS trg_derive_collar_spatial_uncertainty ON silver.collars');
        DB::statement(<<<'SQL'
            CREATE TRIGGER trg_derive_collar_spatial_uncertainty
                BEFORE INSERT OR UPDATE ON silver.collars
                FOR EACH ROW
                EXECUTE FUNCTION silver.derive_collar_spatial_uncertainty();
        SQL);

        // 3. Reconcile rows carrying an invented-value signature. Clearing to
        // NULL is not enough (the row is not re-inserted), so re-derive
        // directly with the Strategy B rule table.
        DB::statement(<<<'SQL'
            UPDATE silver.collars
               SET spatial_uncertainty_m = CASE
                       WHEN georef_method = 'survey' THEN 10.0
                       WHEN georef_method IN ('declared', 'detected', 'manual')
                        AND COALESCE(drill_date::date, created_at::date) >= DATE '2010-01-01'
                           THEN 35.0
                       WHEN georef_method IN ('declared', 'detected', 'manual')
                           THEN 75.0
                       WHEN georef_method = 'assumed' THEN 175.0
                       ELSE NULL
                   END,
                   spatial_uncertainty_method = CASE
                       WHEN georef_method = 'survey' THEN 'government_gps'
                       WHEN georef_method IN ('declared', 'detected', 'manual')
                        AND COALESCE(drill_date::date, created_at::date) >= DATE '2010-01-01'
                           THEN 'modern_ni43101_declared'
                       WHEN georef_method IN ('declared', 'detected', 'manual')
                           THEN 'legacy_declared'
                       WHEN georef_method = 'assumed' THEN 'legacy_assumed_utm'
                       ELSE NULL
                   END
             WHERE (spatial_uncertainty_m = 0.5 AND spatial_uncertainty_method = 'modern_ni43101_survey')
                OR (spatial_uncertainty_m = 5   AND spatial_uncertainty_method = 'modern_ni43101_declared')
                OR (spatial_uncertainty_m = 25  AND spatial_uncertainty_method IN ('legacy_assumed_utm', 'hand_digitised'))
                OR (spatial_uncertainty_m = 50  AND spatial_uncertainty_method = 'legacy_assumed_utm');
        SQL);

        // 4. Standard Strategy B backfill for rows still NULL (mirrors the
        // corrected 2026_06_29 migration; idempotent no-op when none exist).
        DB::statement(<<<'SQL'
            UPDATE silver.collars
               SET spatial_uncertainty_m = CASE
                       WHEN georef_method = 'survey' THEN 10.0
                       WHEN georef_method IN ('declared', 'detected', 'manual')
                        AND COALESCE(drill_date::date, created_at::date) >= DATE '2010-01-01'
                           THEN 35.0
                       WHEN georef_method IN ('declared', 'detected', 'manual')
                           THEN 75.0
                       WHEN georef_method = 'assumed' THEN 175.0
                   END,
                   spatial_uncertainty_method = CASE
                       WHEN georef_method = 'survey' THEN 'government_gps'
                       WHEN georef_method IN ('declared', 'detected', 'manual')
                        AND COALESCE(drill_date::date, created_at::date) >= DATE '2010-01-01'
                           THEN 'modern_ni43101_declared'
                       WHEN georef_method IN ('declared', 'detected', 'manual')
                           THEN 'legacy_declared'
                       WHEN georef_method = 'assumed' THEN 'legacy_assumed_utm'
                   END
             WHERE spatial_uncertainty_m IS NULL
               AND georef_method IN ('survey', 'declared', 'detected', 'manual', 'assumed');
        SQL);
    }

    public function down(): void
    {
        // Intentionally empty: rolling back would restore the invented-value
        // rubric, which was never approved. The corrected 2026_06_29 migration
        // owns the trigger/function lifecycle for full down-migrations.
    }
};
