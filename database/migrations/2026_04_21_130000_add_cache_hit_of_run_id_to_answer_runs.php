<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Migration batch 18 — Module 4 Phase B addendum (cache-scope fix 2026-04-21).
 *
 * Adds cache_hit_of_run_id to silver.answer_runs so the cache audit trail
 * can identify which runs reused a CachedRetrievalContext from a prior run.
 *
 * When cache_hit_of_run_id IS NOT NULL the row was synthesized from a cached
 * retrieval context rather than running fresh retrieval. The FK points at the
 * originating run whose retrieval result was cached.
 *
 * ON DELETE SET NULL: if the originating run is ever purged (e.g., data
 * retention sweep), the FK becomes NULL. The row is still valid — it just
 * loses the back-reference.
 *
 * The partial index keeps the idx small: only rows that ARE cache hits are
 * indexed. Most rows will have NULL and are excluded from the index entirely.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement(
            'ALTER TABLE silver.answer_runs
             ADD COLUMN cache_hit_of_run_id UUID NULL
             REFERENCES silver.answer_runs(answer_run_id) ON DELETE SET NULL',
        );

        DB::statement(
            'CREATE INDEX idx_answer_runs_cache_hit
             ON silver.answer_runs(cache_hit_of_run_id)
             WHERE cache_hit_of_run_id IS NOT NULL',
        );
    }

    public function down(): void
    {
        DB::statement('DROP INDEX IF EXISTS silver.idx_answer_runs_cache_hit');
        DB::statement(
            'ALTER TABLE silver.answer_runs DROP COLUMN IF EXISTS cache_hit_of_run_id',
        );
    }
};
