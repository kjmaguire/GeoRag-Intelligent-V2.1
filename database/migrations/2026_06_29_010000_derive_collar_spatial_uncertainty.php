<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * CC-01 Item 2 — spatial_uncertainty_m backfill + auto-derivation.
 *
 * Implements Strategy B — the ONLY rubric Kyle has approved (2026-05-24),
 * authoritative copy in database/raw/_adhoc/2026_05_30_backfill_spatial_uncertainty.sql:
 *
 *   survey                          → 10 m   government_gps
 *   declared/detected/manual modern → 35 m   modern_ni43101_declared
 *   declared/detected/manual legacy → 75 m   legacy_declared
 *   assumed (any era)               → 175 m  legacy_assumed_utm
 *
 * "Modern" = COALESCE(drill_date, created_at) >= 2010-01-01; a NULL date is
 * conservatively legacy. The modern_ni43101_survey (7 m) and hand_digitised
 * (350 m) tiers exist in Strategy B but are intentionally not wired to any
 * current georef_method value (same as the ad-hoc script).
 *
 * CORRECTION 2026-07-02 (audit remediation): an earlier revision of this file
 * shipped a DIFFERENT value set (0.5/5/25/50 m) under a "Kyle approved
 * (2026-06-29)" comment. That approval never happened — CC-01 Item 2 is
 * blocked on a sign-off for any NEW rubric, and inventing values is
 * prohibited. This revision reverts to the approved Strategy B values and
 * era logic. Because the original revision already ran on the live cluster,
 * migration 2026_07_02_000000_correct_collar_spatial_uncertainty_to_strategy_b
 * re-applies the corrected function/backfill there; this in-place edit covers
 * fresh clusters and the test-parity DB.
 *
 * Implemented as a BEFORE trigger so EVERY future collar gets a defensible
 * uncertainty with zero ingester code changes (same pattern as the
 * bronze.provenance autofill trigger), PLUS a one-time backfill for existing
 * rows.
 *
 * Design guardrails:
 *   - Only DERIVES when spatial_uncertainty_m IS NULL — never clobbers a value
 *     set by a direct measurement (spatial_uncertainty_method stays NULL there).
 *   - georef_method NULL/unknown → leaves spatial_uncertainty_m NULL, which the
 *     map UI correctly renders as "no ring" (per the column comment). We do NOT
 *     invent a ring for genuinely-unrecorded locations.
 *   - The spatial_uncertainty_method audit column is created here (idempotent)
 *     — previously it only existed where the ad-hoc script had been run
 *     manually, which broke fresh clusters and the test-parity DB.
 */
return new class extends Migration
{
    public function getConnection(): ?string
    {
        // Only route to the dedicated owner connection when it's actually
        // opted into (MIGRATE_DB_CONNECTION) — the unconditional
        // `!== 'sqlite'` check broke CI/local test DBs, where
        // `pgsql_migrations` defaults to an unreachable host.
        return config('database.migrations.connection') === 'pgsql_migrations' ? 'pgsql_migrations' : null;
    }

    public function up(): void
    {
        if (config('database.default') === 'sqlite') {
            return;
        }

        // Audit column — copied verbatim from the ad-hoc Strategy B script so
        // fresh clusters no longer depend on that script having been run.
        DB::statement(<<<'SQL'
            ALTER TABLE silver.collars
                ADD COLUMN IF NOT EXISTS spatial_uncertainty_method VARCHAR(100)
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.collars.spatial_uncertainty_method IS
                'CC-01 Item 2 — rule name used to derive spatial_uncertainty_m. '
                'NULL when uncertainty was set by a direct measurement or not yet assigned. '
                'One of: modern_ni43101_survey, modern_ni43101_declared, legacy_declared, '
                'legacy_assumed_utm, hand_digitised, government_gps.'
        SQL);

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.derive_collar_spatial_uncertainty()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                is_modern boolean;
            BEGIN
                -- Derive ONLY when not already set (don't overwrite a direct
                -- measurement) and when we have a method to key off.
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
                    -- Unmapped georef_method → leave NULL (do not guess; UI
                    -- omits the ring). Matches the ad-hoc script's ELSE NULL.
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

        // One-time backfill for existing rows — same Strategy B rule table.
        // CASE order matters: the "modern" branch's date comparison is NULL
        // when both dates are NULL, so such rows fall through to the legacy
        // branch (conservative, per Strategy B).
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
        if (config('database.default') === 'sqlite') {
            return;
        }
        DB::statement('DROP TRIGGER IF EXISTS trg_derive_collar_spatial_uncertainty ON silver.collars');
        DB::statement('DROP FUNCTION IF EXISTS silver.derive_collar_spatial_uncertainty()');
        // The spatial_uncertainty_method column is deliberately NOT dropped:
        // it is data-bearing and shared with the ad-hoc Strategy B script.
    }
};
