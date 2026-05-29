<?php

/**
 * B4 — Module 4 Phase B Chunk 3: Add partial_failure_details to silver.answer_runs.
 *
 * When the parallel fan-out (asyncio.gather across Qdrant / Neo4j / PostGIS)
 * has a partial failure (one or more stores time out or error), the orchestrator
 * persists a JSONB dict of {store: exception_class} to this column for
 * observability. NULL means all stores responded successfully.
 *
 * Example value:
 *   {"qdrant": "TimeoutError", "neo4j": "ServiceUnavailable"}
 *
 * This is additive — no existing rows are affected; NULL is the default.
 * Rollback: DROP COLUMN (safe, no FK references to this column).
 */

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    public function up(): void
    {
        DB::statement(
            'ALTER TABLE silver.answer_runs
             ADD COLUMN IF NOT EXISTS partial_failure_details JSONB NULL'
        );

        // Partial index for observability queries: "show me runs that had failures".
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_answer_runs_partial_failures
             ON silver.answer_runs (workspace_id)
             WHERE partial_failure_details IS NOT NULL'
        );
    }

    public function down(): void
    {
        DB::statement(
            'DROP INDEX IF EXISTS silver.idx_answer_runs_partial_failures'
        );
        DB::statement(
            'ALTER TABLE silver.answer_runs
             DROP COLUMN IF EXISTS partial_failure_details'
        );
    }
};
