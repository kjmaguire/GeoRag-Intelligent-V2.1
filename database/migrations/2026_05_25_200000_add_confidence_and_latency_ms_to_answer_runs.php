<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * RetrievalInspector follow-up — persist `confidence` and `latency_ms` on
 * `silver.answer_runs`.
 *
 * Why these columns
 * -----------------
 * The Retrieval Inspector (Foundry → "inspect retrieval →") reads both
 * values out of `silver.answer_runs` and renders them in the page header
 * ("conf 0.87 · 6420ms"). Until this migration the columns did not exist;
 * the controller fell back to NULL via `?? null` and the UI always showed
 * `conf — · —ms` even for fully successful runs.
 *
 * `confidence` is the composite hallucination-prevention score returned on
 * `GeoRAGResponse.confidence` (Layers 1-6 per §04i). Range [0, 1]. NULL when
 * the run was rejected before any answer was assembled or when the legacy
 * orchestrator path wrote a row without populating it.
 *
 * `latency_ms` is the wall-clock duration of `run_deterministic_rag`
 * measured at function entry via `time.monotonic()` — covers cache lookup,
 * tool fan-out, LLM synthesis, span resolution, and guard validation.
 * NULL on legacy rows that predate this migration.
 *
 * Both columns are nullable so the existing 18k+ committed rows do not need
 * a backfill. CHECK constraints mirror the `GeoRAGResponse` validator
 * (`0.0 ≤ confidence ≤ 1.0`, `latency_ms ≥ 0`) so the database catches any
 * future writer that drifts from the contract.
 *
 * The orchestrator writes both values in the post-INSERT UPDATE block
 * (see `src/fastapi/app/agent/orchestrator/__init__.py`, "Patch the run
 * row with all metadata fields"). Refusal-path INSERTs also populate them
 * — `confidence = 0.0` plus a measured `latency_ms`.
 *
 * SQLite (test DB) — gated on Postgres. The Inspector tests run against
 * the pgsql phpunit config; SQLite tests do not exercise this surface.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE silver.answer_runs
              ADD COLUMN IF NOT EXISTS confidence numeric(5,4),
              ADD COLUMN IF NOT EXISTS latency_ms integer
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE silver.answer_runs
              DROP CONSTRAINT IF EXISTS answer_runs_confidence_range
        SQL);
        DB::statement(<<<'SQL'
            ALTER TABLE silver.answer_runs
              ADD CONSTRAINT answer_runs_confidence_range
              CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0))
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE silver.answer_runs
              DROP CONSTRAINT IF EXISTS answer_runs_latency_ms_nonneg
        SQL);
        DB::statement(<<<'SQL'
            ALTER TABLE silver.answer_runs
              ADD CONSTRAINT answer_runs_latency_ms_nonneg
              CHECK (latency_ms IS NULL OR latency_ms >= 0)
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.answer_runs.confidence IS
              'Composite §04i hallucination-prevention confidence on the assembled answer (0.0-1.0). NULL when the row predates the answer-assembly stage or the orchestrator did not populate it. Surfaced on the Retrieval Inspector header.'
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.answer_runs.latency_ms IS
              'Wall-clock duration of run_deterministic_rag in milliseconds (cache lookup + tool fan-out + LLM + guards). Measured via time.monotonic(). NULL on legacy rows. Surfaced on the Retrieval Inspector header.'
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement('ALTER TABLE silver.answer_runs DROP CONSTRAINT IF EXISTS answer_runs_confidence_range');
        DB::statement('ALTER TABLE silver.answer_runs DROP CONSTRAINT IF EXISTS answer_runs_latency_ms_nonneg');
        DB::statement('ALTER TABLE silver.answer_runs DROP COLUMN IF EXISTS confidence');
        DB::statement('ALTER TABLE silver.answer_runs DROP COLUMN IF EXISTS latency_ms');
    }
};
