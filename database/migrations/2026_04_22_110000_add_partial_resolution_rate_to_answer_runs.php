<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Module 6 Phase B Chunk 4b — OFR-3 resolution.
 *
 * Adds `partial_resolution_rate NUMERIC(5,4) NULL` to silver.answer_runs.
 *
 * Semantics
 * ---------
 * Populated by the orchestrator at the same time `citation_mode` is written
 * (Stage 2 of the two-stage citation pipeline).  Value is:
 *   markers_resolved / unique_markers  (0.0 when unique_markers == 0)
 *
 * 1.0 = all markers resolved (fully_resolved=true path).
 * 0.0 when no markers were found (not a failure — some queries produce
 *     data-only answers with no document citations).
 * NULL = span resolver did not run (flag=false or LLM failed).
 *
 * NUMERIC(5,4): range 0.0000–1.0000 with 4 decimal places.
 * NULL-capable so existing rows stay NULL (no backfill needed).
 *
 * Reversibility: additive + reversible — DROP COLUMN IF EXISTS is safe.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement(
            'ALTER TABLE silver.answer_runs '
            .'ADD COLUMN IF NOT EXISTS partial_resolution_rate NUMERIC(5,4) NULL',
        );
    }

    public function down(): void
    {
        DB::statement(
            'ALTER TABLE silver.answer_runs '
            .'DROP COLUMN IF EXISTS partial_resolution_rate',
        );
    }
};
