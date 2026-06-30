<?php

/**
 * Module 6 Phase B Chunk 1 — Create silver.answer_citation_items.
 *
 * Date: 2026-04-21.
 *
 * Purpose
 * -------
 * Per-citation anchor table.  One row per unique citation-marker per
 * answer run.  Binds a marker string (e.g. [DATA-1] or [ev:a1b2c3d4]) to
 * the canonical evidence_id / passage_id that backs the claim.
 *
 * This is the anchor for Stage 2 span resolution (Chunk 2).  Multiple span
 * occurrences of the same marker fan out into answer_citation_spans (migration
 * 160000).  Guards (Chunk 3) write rejection_reason when a marker fails.
 *
 * Chunk 1 dual-support window
 * ---------------------------
 * marker_text accepts both formats:
 *   - Legacy tool-slot:  [DATA-N] | [NI43-N] | [PUB-N] | [PGEO-N]
 *   - Future evidence-id: [ev:<first-8-chars-of-evidence-uuid>]
 * Chunk 2 migrates the pipeline to evidence-id markers; this CHECK is
 * permissive for now and will be tightened in a later chunk.
 *
 * FK graph
 * --------
 *   answer_citation_items.answer_run_id   → silver.answer_runs.answer_run_id (CASCADE)
 *   answer_citation_items.workspace_id    → silver.workspaces.workspace_id   (CASCADE)
 *   answer_citation_items.evidence_id     → silver.evidence_items.evidence_id (SET NULL)
 *   answer_citation_items.passage_id      → silver.document_passages.passage_id (SET NULL)
 *
 * At least one of (evidence_id, passage_id) must be non-NULL (CHECK enforced).
 * evidence_id is nullable because legacy [DATA-N] markers predate the evidence_id
 * write path; passage_id covers the Chunk 1 transition window.
 *
 * NOT NULL enforcement on evidence_id is deferred to B8.7 (application layer
 * only — never at DB level until write path is stable in production).
 *
 * Rollback
 * --------
 * answer_citation_spans (migration 160000) references answer_citation_item_id.
 * Its down() runs first in reverse-batch order.  This down() drops
 * answer_citation_items only.
 */

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    public function up(): void
    {
        // -----------------------------------------------------------------------
        // Create silver.answer_citation_items
        // -----------------------------------------------------------------------
        DB::statement(
            'CREATE TABLE IF NOT EXISTS silver.answer_citation_items (
                answer_citation_item_id  UUID         NOT NULL DEFAULT gen_random_uuid(),
                answer_run_id            UUID         NOT NULL
                    REFERENCES silver.answer_runs(answer_run_id) ON DELETE CASCADE,
                workspace_id             UUID         NOT NULL
                    REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,

                -- Target of the citation.
                -- evidence_id is the canonical FK (§04j); passage_id is kept for
                -- the Chunk 2 dual-support window so legacy passage-type citations
                -- can still be written while the migration to evidence_id is in flight.
                -- Both nullable; CHECK ensures at least one is populated.
                evidence_id              UUID         NULL
                    REFERENCES silver.evidence_items(evidence_id) ON DELETE SET NULL,
                passage_id               UUID         NULL
                    REFERENCES silver.document_passages(passage_id) ON DELETE SET NULL,

                -- The marker as emitted in the answer text.  Chunk 1 accepts both formats:
                --   Legacy: [DATA-N] | [NI43-N] | [PUB-N] | [PGEO-N]
                --   Future: [ev:<first-8-chars-of-evidence-uuid>]
                -- Chunk 2 migration bumps the marker format and tightens this CHECK.
                marker_text              VARCHAR(64)  NOT NULL,

                -- Source-store hint mirrors answer_retrieval_items.source_store.
                -- Nullable because legacy [DATA-N] markers may predate populated evidence_id.
                source_store             VARCHAR(16)  NULL,

                -- Per-citation confidence score (emitted by the span resolver in Chunk 2).
                confidence               NUMERIC(5,4) NULL,

                -- Rejection reason (set by Chunk 3 guards if a marker fails validation).
                rejection_reason         VARCHAR(128) NULL,

                -- Timestamps.
                created_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

                CONSTRAINT answer_citation_items_pkey
                    PRIMARY KEY (answer_citation_item_id),

                -- At least one of (evidence_id, passage_id) must be populated.
                CONSTRAINT answer_citation_items_has_target
                    CHECK (evidence_id IS NOT NULL OR passage_id IS NOT NULL),

                -- Source store within the allowed set (mirrors answer_retrieval_items).
                CONSTRAINT answer_citation_items_source_store_valid
                    CHECK (source_store IS NULL OR source_store IN (
                        \'qdrant\', \'neo4j\', \'postgis\', \'hybrid\'
                    )),

                -- Marker format permissive in Chunk 1; Chunk 2 tightens it.
                CONSTRAINT answer_citation_items_marker_shape
                    CHECK (marker_text ~ \'^\[(DATA|NI43|PUB|PGEO|ev):[A-Za-z0-9-]+\]$\'),

                -- One citation_item per (run, marker) — a marker stands for one
                -- evidence-binding decision within the run.  Multiple spans can reference
                -- the same item (one marker, many occurrences in the answer text) — that
                -- fans out into answer_citation_spans.
                CONSTRAINT answer_citation_items_unique_per_run
                    UNIQUE (answer_run_id, marker_text)
            )',
        );

        // -----------------------------------------------------------------------
        // Indices
        // -----------------------------------------------------------------------
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_answer_citation_items_run
                 ON silver.answer_citation_items (answer_run_id)',
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_answer_citation_items_workspace
                 ON silver.answer_citation_items (workspace_id)',
        );

        // Partial: only rows where evidence_id is populated (citation traceability).
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_answer_citation_items_evidence
                 ON silver.answer_citation_items (evidence_id)
                 WHERE evidence_id IS NOT NULL',
        );

        // Partial: only rows where passage_id is populated (legacy path).
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_answer_citation_items_passage
                 ON silver.answer_citation_items (passage_id)
                 WHERE passage_id IS NOT NULL',
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_answer_citation_items_marker_text
                 ON silver.answer_citation_items (marker_text)',
        );
    }

    public function down(): void
    {
        // answer_citation_spans (migration 160000) references answer_citation_item_id.
        // Its down() runs first in reverse-batch order.  This down() only drops
        // the answer_citation_items table itself.
        DB::statement('DROP TABLE IF EXISTS silver.answer_citation_items');
    }
};
