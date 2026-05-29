<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Plan §3 context-prep audit + §3e multi-turn resolution columns.
 *
 * Adds two JSONB columns to silver.query_traces:
 *
 *   context_prep_audit — populated by persist_node when CONTEXT_PREP_ENABLED
 *     is True. Shape:
 *       {
 *         "intent": "synthesis",
 *         "quota_used": {"document": 3, "spatial": 2, ...},
 *         "reached_budget": true,
 *         "dropped_evidence_ids": ["...", "..."],
 *         "budget_reason": null,
 *         "kind_distribution_before": {"document": 5, "spatial": 2},
 *         "kind_distribution_after":  {"document": 3, "spatial": 2}
 *       }
 *     Closes the "NOT yet wired" gap in context_prep_spec.md §6.
 *
 *   multi_turn_resolution — populated by persist_node when
 *     MULTI_TURN_RESOLUTION_ENABLED is True. Shape:
 *       {
 *         "original_query":  "what are ITS top assays?",
 *         "rewritten_query": "what are PLS-22-08's top assays?",
 *         "trace": [
 *           {"kind": "pronoun", "original_phrase": "ITS",
 *            "resolved_to": "PLS-22-08", "source_turn_index": 0,
 *            "confidence": 0.85}
 *         ],
 *         "overall_confidence": 0.85
 *       }
 *     Closes the gap in multi_turn_resolution_spec.md §7.
 *
 * Both columns are nullable + default NULL. When the respective flag is
 * off, persist_node skips the stamp — no schema change required for the
 * Stage 1 (shadow) state.
 *
 * Indexes: GIN on each column so trace-inspector queries like
 *   WHERE context_prep_audit ? 'budget_reason'
 *   WHERE multi_turn_resolution -> 'trace' @> '[{"kind":"pronoun"}]'::jsonb
 * run efficiently on a million-row table.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE silver.query_traces
                ADD COLUMN IF NOT EXISTS context_prep_audit JSONB
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE silver.query_traces
                ADD COLUMN IF NOT EXISTS multi_turn_resolution JSONB
        SQL);

        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_query_traces_context_prep_audit
                ON silver.query_traces USING GIN (context_prep_audit)
                WHERE context_prep_audit IS NOT NULL
        SQL);

        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_query_traces_multi_turn_resolution
                ON silver.query_traces USING GIN (multi_turn_resolution)
                WHERE multi_turn_resolution IS NOT NULL
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP INDEX IF EXISTS silver.idx_query_traces_multi_turn_resolution');
        DB::statement('DROP INDEX IF EXISTS silver.idx_query_traces_context_prep_audit');
        DB::statement('ALTER TABLE silver.query_traces DROP COLUMN IF EXISTS multi_turn_resolution');
        DB::statement('ALTER TABLE silver.query_traces DROP COLUMN IF EXISTS context_prep_audit');
    }
};
