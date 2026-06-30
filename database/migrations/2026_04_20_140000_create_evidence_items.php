<?php

/**
 * B8.1 (part 2) — EVID: Create silver.evidence_items per addendum §04j.
 *
 * Module 3 Phase B 2026-04-20.  DRAFT — senior-reviewer (Opus) must approve
 * before php artisan migrate is run.
 *
 * Purpose
 * -------
 * The unified evidence substrate.  Every citable unit of knowledge — whether it
 * is a passage from a PDF, a row in a structured table, an edge in the Neo4j
 * graph, or a feature on a map tile — gets one row here with a stable UUID.
 * answer_citation_items (Module 6) will reference evidence_id to attach
 * citations to answers.
 *
 * Discriminator design
 * --------------------
 * evidence_type is VARCHAR(32) with a CHECK constraint enumerating the four
 * valid values.  Rationale: zero existing CREATE TYPE … AS ENUM found across
 * all 36 migrations (grep confirmed).  PostgreSQL ENUMs require DDL to add new
 * values (ALTER TYPE … ADD VALUE) and cannot be rolled back without recreating
 * the type.  CHECK + VARCHAR is additive — new evidence types can be added by
 * widening the CHECK in a future migration without touching existing rows.
 *
 * Type-consistency CHECK
 * ----------------------
 * A second CHECK constraint enforces that the populated *_ref column matches
 * the declared evidence_type.  This prevents mis-labelled rows at the database
 * layer independently of application logic.
 *
 * Mutual-exclusion CHECK
 * ----------------------
 * Exactly one of (passage_id, structured_ref, graph_edge_ref, map_feature_ref)
 * must be non-NULL.  The CASE-sum pattern is the canonical PostgreSQL approach
 * for this; it avoids XOR across nullable columns.
 *
 * FK graph (parent tables must already exist):
 *   evidence_items.workspace_id → silver.workspaces.workspace_id (CASCADE DELETE)
 *   evidence_items.passage_id   → silver.document_passages.passage_id (RESTRICT)
 *
 * document_revisions must exist before this migration runs (130000 batch).
 *
 * Rollback: DROP TABLE evidence_items.  structured_record_lineage (150000) must
 * be dropped first in rollback sequence.
 *
 * NOT in this migration
 * ---------------------
 * - answer_citation_items  (Module 6 scope)
 * - evidence_id FK on any existing table  (Module 6 scope)
 * - B8.5 behavioral enable  (gated on Module 6 readiness)
 */

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    public function up(): void
    {
        // -----------------------------------------------------------------------
        // Create silver.evidence_items
        // -----------------------------------------------------------------------
        DB::statement(
            'CREATE TABLE IF NOT EXISTS silver.evidence_items (
                evidence_id         UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id        UUID         NOT NULL
                    REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                evidence_type       VARCHAR(32)  NOT NULL,
                passage_id          UUID         NULL
                    REFERENCES silver.document_passages(passage_id) ON DELETE RESTRICT,
                -- RESTRICT (not SET NULL) per senior-reviewer 2026-04-20: SET NULL would
                -- leave evidence_type=document_passage with passage_id IS NULL, violating
                -- both evidence_items_type_ref_consistent and evidence_items_exactly_one_ref.
                -- RESTRICT forces ingestion code to explicitly migrate evidence rows before
                -- pruning a passage, protecting Invariant 1 (citation-first).
                structured_ref      JSONB        NULL,
                graph_edge_ref      JSONB        NULL,
                map_feature_ref     JSONB        NULL,
                source_uri          TEXT         NOT NULL,
                source_date         DATE         NULL,
                linked_node_ids     JSONB        NULL,
                created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

                CONSTRAINT evidence_items_pkey
                    PRIMARY KEY (evidence_id),

                -- Discriminator must be one of the four recognised evidence types.
                CONSTRAINT evidence_items_type_valid
                    CHECK (evidence_type IN (
                        \'document_passage\',
                        \'structured_record\',
                        \'graph_edge\',
                        \'map_feature\'
                    )),

                -- Exactly one ref column must be non-NULL.
                CONSTRAINT evidence_items_exactly_one_ref
                    CHECK (
                        (CASE WHEN passage_id     IS NOT NULL THEN 1 ELSE 0 END +
                         CASE WHEN structured_ref IS NOT NULL THEN 1 ELSE 0 END +
                         CASE WHEN graph_edge_ref IS NOT NULL THEN 1 ELSE 0 END +
                         CASE WHEN map_feature_ref IS NOT NULL THEN 1 ELSE 0 END) = 1
                    ),

                -- Type-consistency: the populated *_ref column must match evidence_type.
                -- Prevents mis-labelled rows independently of application logic.
                CONSTRAINT evidence_items_type_ref_consistent
                    CHECK (
                        (evidence_type = \'document_passage\' AND passage_id     IS NOT NULL) OR
                        (evidence_type = \'structured_record\' AND structured_ref IS NOT NULL) OR
                        (evidence_type = \'graph_edge\'        AND graph_edge_ref IS NOT NULL) OR
                        (evidence_type = \'map_feature\'       AND map_feature_ref IS NOT NULL)
                    )
            )',
        );

        // -----------------------------------------------------------------------
        // Indices per §04j spec
        // -----------------------------------------------------------------------
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_evidence_items_workspace_id
                 ON silver.evidence_items (workspace_id)',
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_evidence_items_evidence_type
                 ON silver.evidence_items (evidence_type)',
        );

        // Partial index: only rows where a passage is linked.
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_evidence_items_passage_id
                 ON silver.evidence_items (passage_id)
                 WHERE passage_id IS NOT NULL',
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_evidence_items_source_date
                 ON silver.evidence_items (source_date)',
        );
    }

    public function down(): void
    {
        // structured_record_lineage (migration 150000) references evidence_id.
        // It must be dropped first (its own down() runs first in reverse-batch order).
        // This down() only needs to drop the evidence_items table itself.
        DB::statement('DROP TABLE IF EXISTS silver.evidence_items');
    }
};
