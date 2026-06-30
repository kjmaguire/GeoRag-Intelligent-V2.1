<?php

/**
 * B1.2 — Module 4 Phase B Chunk 1: Create silver.answer_retrieval_items.
 *
 * Date: 2026-04-21.
 *
 * Purpose
 * -------
 * Per-candidate retrieval trace log.  One row per retrieval candidate per stage
 * per answer run.  Enables:
 *   - Offline evaluation of retrieval precision/recall against golden queries
 *   - Module 6 citation traceability (which passage reached in_context / cited?)
 *   - Hallucination audit (was the cited chunk actually retrieved and reranked?)
 *   - RRF score debugging (rrf_rank + rrf_score visible per candidate)
 *
 * Stages:
 *   retrieved   — candidate returned from Qdrant / PostGIS / Neo4j before rerank
 *   reranked    — candidate survived the cross-encoder rerank pass
 *   in_context  — candidate was included in the LLM prompt context block
 *   cited       — candidate was referenced in a citation in the final answer
 *
 * FK graph (parents must exist before this migration):
 *   answer_retrieval_items.answer_run_id        → silver.answer_runs.answer_run_id (CASCADE)
 *   answer_retrieval_items.workspace_id         → silver.workspaces.workspace_id   (CASCADE)
 *   answer_retrieval_items.document_revision_id → silver.document_revisions.document_revision_id (SET NULL)
 *   answer_retrieval_items.passage_id           → silver.document_passages.passage_id (SET NULL)
 *
 * At least one of (document_revision_id, passage_id, candidate_ref) should be
 * populated per row.  The DB does not enforce a CHECK here because structured
 * candidates (PostGIS rows, graph edges, map features) legitimately populate
 * only candidate_ref.  Application code is responsible for ensuring provenance.
 *
 * ENUM vs CHECK
 * -------------
 * VARCHAR + CHECK for stage and source_store, matching the pattern in
 * evidence_items (migration 140000) and answer_runs (migration 100000).
 *
 * Rollback: drop this table before answer_runs (reverse-batch order).
 */

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    public function up(): void
    {
        // -----------------------------------------------------------------------
        // Create silver.answer_retrieval_items
        // -----------------------------------------------------------------------
        DB::statement(
            'CREATE TABLE IF NOT EXISTS silver.answer_retrieval_items (
                retrieval_item_id    UUID          NOT NULL DEFAULT gen_random_uuid(),
                answer_run_id        UUID          NOT NULL
                    REFERENCES silver.answer_runs(answer_run_id) ON DELETE CASCADE,
                workspace_id         UUID          NOT NULL
                    REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                stage                VARCHAR(16)   NOT NULL,
                source_store         VARCHAR(16)   NOT NULL,

                -- What the candidate points at (mutually optional; at least one populated)
                document_revision_id UUID          NULL
                    REFERENCES silver.document_revisions(document_revision_id) ON DELETE SET NULL,
                passage_id           UUID          NULL
                    REFERENCES silver.document_passages(passage_id) ON DELETE SET NULL,
                -- For non-passage candidates (structured row, graph edge, map feature)
                candidate_ref        JSONB         NULL,

                -- Scores
                retriever_score      NUMERIC(10,6) NULL,
                reranker_score       NUMERIC(10,6) NULL,
                rrf_rank             INTEGER       NULL,
                rrf_score            NUMERIC(10,6) NULL,

                -- Inclusion flags
                included_in_context  BOOLEAN       NOT NULL DEFAULT false,
                used_in_citation     BOOLEAN       NOT NULL DEFAULT false,

                created_at           TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

                CONSTRAINT answer_retrieval_items_pkey
                    PRIMARY KEY (retrieval_item_id),

                CONSTRAINT answer_retrieval_items_stage_valid
                    CHECK (stage IN (\'retrieved\', \'reranked\', \'in_context\', \'cited\')),

                CONSTRAINT answer_retrieval_items_store_valid
                    CHECK (source_store IN (\'qdrant\', \'neo4j\', \'postgis\', \'hybrid\'))
            )',
        );

        // -----------------------------------------------------------------------
        // Indices
        // -----------------------------------------------------------------------
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_retrieval_items_run
                 ON silver.answer_retrieval_items (answer_run_id)',
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_retrieval_items_workspace
                 ON silver.answer_retrieval_items (workspace_id)',
        );

        // Composite: supports "give me all reranked candidates for this run" queries.
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_retrieval_items_stage
                 ON silver.answer_retrieval_items (answer_run_id, stage)',
        );

        // Partial index: only rows tied to a specific passage (citation traceability).
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_retrieval_items_passage
                 ON silver.answer_retrieval_items (passage_id)
                 WHERE passage_id IS NOT NULL',
        );
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.answer_retrieval_items');
    }
};
