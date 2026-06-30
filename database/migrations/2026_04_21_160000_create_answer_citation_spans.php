<?php

/**
 * Module 6 Phase B Chunk 1 — Create silver.answer_citation_spans.
 *
 * Date: 2026-04-21.
 *
 * Purpose
 * -------
 * Per-occurrence table for citation markers within the answer text.  One row
 * per marker instance (by character offset).  A single citation_item can map
 * to many spans — e.g. [DATA-1] appearing three times in the answer text
 * produces three span rows, all pointing at the same citation_item.
 *
 * The span resolver (Chunk 2) walks the final answer text, finds all
 * occurrences of each resolved marker, computes (span_start, span_end) as
 * character offsets, and writes these rows.  Chunk 1 only creates the table;
 * no rows are written until the Chunk 2 span resolver lands.
 *
 * FK graph
 * --------
 *   answer_citation_spans.answer_run_id            → silver.answer_runs.answer_run_id (CASCADE)
 *   answer_citation_spans.answer_citation_item_id  → silver.answer_citation_items.answer_citation_item_id (CASCADE)
 *   answer_citation_spans.workspace_id             → silver.workspaces.workspace_id (CASCADE)
 *
 * Rollback
 * --------
 * Drop this table before answer_citation_items (reverse-batch order).
 */

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    public function up(): void
    {
        // -----------------------------------------------------------------------
        // Create silver.answer_citation_spans
        // -----------------------------------------------------------------------
        DB::statement(
            'CREATE TABLE IF NOT EXISTS silver.answer_citation_spans (
                answer_citation_span_id   UUID        NOT NULL DEFAULT gen_random_uuid(),
                answer_run_id             UUID        NOT NULL
                    REFERENCES silver.answer_runs(answer_run_id) ON DELETE CASCADE,
                answer_citation_item_id   UUID        NOT NULL
                    REFERENCES silver.answer_citation_items(answer_citation_item_id) ON DELETE CASCADE,
                workspace_id              UUID        NOT NULL
                    REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,

                -- Character offsets (0-based, exclusive end) within the final answer text.
                span_start                INTEGER     NOT NULL,
                span_end                  INTEGER     NOT NULL,

                created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                CONSTRAINT answer_citation_spans_pkey
                    PRIMARY KEY (answer_citation_span_id),

                -- span_end must be strictly after span_start; span_start must be non-negative.
                CONSTRAINT answer_citation_spans_range_valid
                    CHECK (span_end > span_start AND span_start >= 0)
            )',
        );

        // -----------------------------------------------------------------------
        // Indices
        // -----------------------------------------------------------------------
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_answer_citation_spans_run
                 ON silver.answer_citation_spans (answer_run_id)',
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_answer_citation_spans_item
                 ON silver.answer_citation_spans (answer_citation_item_id)',
        );

        // Composite: supports ordered rendering of citation chips by position.
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_answer_citation_spans_run_start
                 ON silver.answer_citation_spans (answer_run_id, span_start)',
        );
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.answer_citation_spans');
    }
};
