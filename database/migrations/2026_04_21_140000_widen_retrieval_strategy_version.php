<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Widen silver.answer_runs.retrieval_strategy_version VARCHAR(32) → VARCHAR(64).
 *
 * Context: TOOL-CALL-01 fix 2026-04-21 found that the value
 * "v3.1-thinking-off-synthesis-2026-04-21" (38 chars) was rejected at INSERT
 * because the column was capped at 32 chars.  The value had to be shortened to
 * "v3.1-think-off-2026-04-21" (25 chars) to fit.  VARCHAR(64) accommodates all
 * plausible version strings for the foreseeable future.
 *
 * PostgreSQL 18 ALTER COLUMN TYPE VARCHAR widening is online and non-locking
 * (no table rewrite needed — VARCHAR widening is a metadata-only change).
 *
 * Module 10 backlog: "answer_runs.retrieval_strategy_version VARCHAR(32) too narrow"
 * Raised: 2026-04-21 (Module 5 TOOL-CALL-01 fix)
 * Resolved: 2026-04-21 (cross-module cleanup sweep Item 7)
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement(
            'ALTER TABLE silver.answer_runs
             ALTER COLUMN retrieval_strategy_version TYPE VARCHAR(64)'
        );
    }

    public function down(): void
    {
        // Truncation-safe: any existing value >32 chars would be lost on rollback.
        // Since current live values are ≤25 chars, truncation is a no-op today.
        DB::statement(
            'ALTER TABLE silver.answer_runs
             ALTER COLUMN retrieval_strategy_version TYPE VARCHAR(32)'
        );
    }
};
