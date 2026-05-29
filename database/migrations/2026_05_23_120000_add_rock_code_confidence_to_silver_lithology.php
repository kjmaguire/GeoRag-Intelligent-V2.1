<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * silver.lithology.rock_code_confidence — fuzzy-match score column.
 *
 * Context (CC-02 Item 1, 2026-05-23): historic lithology imports collapse
 * a long tail of free-text rock names against silver.rock_codes via exact
 * lowercase match. 1,000+ vendor codes vs ~30 seeded entries means the
 * vast majority of imports leave rock_code NULL today.
 *
 * The bronze_to_silver/lithology asset now falls back to rapidfuzz when
 * the exact lookup misses. This column records the certainty of that
 * match so geologists can:
 *   - filter the lakehouse for low-confidence rows that need review
 *   - sort the review queue by uncertainty
 *   - distinguish "exact catalogue hit" (1.0) from "fuzzy guess" (<1.0)
 *
 * Encoding:
 *   1.0       — exact catalogue match (preserves existing semantics)
 *   [0.6,1.0) — fuzzy match accepted; needs human eyes before promotion
 *   NULL      — no match (catalogue gap; rock_code is NULL too)
 *
 * Default threshold for "accept fuzzy" is configured in the Dagster
 * asset (preferred_match_threshold) so it can be tuned per-workspace
 * without a schema change.
 *
 * SQLite (test DB) — gated on Postgres. silver.lithology itself only
 * exists on Postgres; no SQLite sibling-provision needed.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE silver.lithology
              ADD COLUMN IF NOT EXISTS rock_code_confidence real NULL
              CHECK (
                rock_code_confidence IS NULL
                OR (rock_code_confidence >= 0.0 AND rock_code_confidence <= 1.0)
              )
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.lithology.rock_code_confidence IS
              'Confidence of the rock_name→rock_code resolution. 1.0 = exact catalogue match. <1.0 = rapidfuzz fuzzy match (see bronze_to_silver/lithology.py preferred_match_threshold). NULL = no match (catalogue gap).'
        SQL);

        // Partial index on low-confidence rows so the lithology review
        // queue (Item 1 follow-up UI) can scan only the rows that need
        // human attention without a sequential scan over all of silver.lithology.
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS silver_lithology_low_confidence_idx
              ON silver.lithology (workspace_id, rock_code_confidence)
              WHERE rock_code_confidence IS NOT NULL
                AND rock_code_confidence < 1.0
        SQL);

        // Trigram index on silver.rock_codes.name so SQL-side fuzzy
        // queries (lithology review UI, ad-hoc admin queries) can use
        // similarity() / %% without a sequential scan. The asset itself
        // does the match in-process via rapidfuzz over the loaded
        // catalogue — this index is for the UI/admin side.
        DB::statement(<<<'SQL'
            CREATE EXTENSION IF NOT EXISTS pg_trgm
        SQL);

        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS silver_rock_codes_name_trgm_idx
              ON silver.rock_codes
              USING gin (lower(name) gin_trgm_ops)
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement('DROP INDEX IF EXISTS silver.silver_rock_codes_name_trgm_idx');
        DB::statement('DROP INDEX IF EXISTS silver.silver_lithology_low_confidence_idx');
        DB::statement('ALTER TABLE silver.lithology DROP COLUMN IF EXISTS rock_code_confidence');
    }
};
