<?php

/**
 * B2 — PASG: Create silver.document_passages per spec §10p-i and §6 B7.
 *
 * Module 3 Phase B 2026-04-20.
 *
 * Passage store shape (§10p-i):
 *   - passage_id        UUID PK (stable; derived from text_hash at write time by the asset)
 *   - document_id       UUID FK → silver.reports.report_id (nullable — allows pre-seeding)
 *   - workspace_id      UUID FK → silver.workspaces.workspace_id NOT NULL (Global Invariant 12)
 *   - revision_number   INTEGER NOT NULL
 *   - text              TEXT NOT NULL
 *   - text_hash         CHAR(64) NOT NULL (SHA-256 hex lowercase)
 *   - ordinal           INTEGER NOT NULL (passage position within document)
 *   - embedding_id      TEXT NULL (Qdrant point ID; written after embedding lands)
 *   - created_at, updated_at
 *
 * Unique constraint: (document_id, revision_number, text_hash) — §10p-i revision-stability.
 * Indices: text_hash, (document_id, revision_number), workspace_id, embedding_id.
 *
 * No backfill from silver.reports.embedding_ids — that is a separate asset-level
 * rewrite scheduled for a later Phase B step.
 *
 * workspaces table must exist (migration 2026_04_20_100000 runs first).
 */

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    public function up(): void
    {
        // -----------------------------------------------------------------------
        // Create silver.document_passages
        // -----------------------------------------------------------------------
        DB::statement(
            'CREATE TABLE IF NOT EXISTS silver.document_passages (
                passage_id       UUID         NOT NULL DEFAULT gen_random_uuid(),
                document_id      UUID         NULL
                    REFERENCES silver.reports(report_id) ON DELETE CASCADE,
                workspace_id     UUID         NOT NULL
                    REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                revision_number  INTEGER      NOT NULL,
                text             TEXT         NOT NULL,
                text_hash        CHAR(64)     NOT NULL,
                ordinal          INTEGER      NOT NULL,
                embedding_id     TEXT         NULL,
                created_at       TIMESTAMP(0) WITHOUT TIME ZONE,
                updated_at       TIMESTAMP(0) WITHOUT TIME ZONE,
                CONSTRAINT document_passages_pkey PRIMARY KEY (passage_id),
                CONSTRAINT document_passages_text_hash_format
                    CHECK (text_hash ~ \'^[0-9a-f]{64}$\'),
                CONSTRAINT document_passages_revision_number_positive
                    CHECK (revision_number >= 1),
                CONSTRAINT document_passages_ordinal_non_negative
                    CHECK (ordinal >= 0),
                CONSTRAINT document_passages_unique_revision_text
                    UNIQUE (document_id, revision_number, text_hash)
            )'
        );

        // -----------------------------------------------------------------------
        // Indices per §10p-i
        // -----------------------------------------------------------------------
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_document_passages_text_hash
                 ON silver.document_passages (text_hash)'
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_document_passages_doc_revision
                 ON silver.document_passages (document_id, revision_number)'
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_document_passages_workspace_id
                 ON silver.document_passages (workspace_id)'
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_document_passages_embedding_id
                 ON silver.document_passages (embedding_id)
                 WHERE embedding_id IS NOT NULL'
        );
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.document_passages');
    }
};
