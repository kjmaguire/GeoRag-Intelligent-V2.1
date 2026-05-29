<?php

/**
 * B8.1 (part 1) — EVID: Create silver.document_revisions per addendum §04j.
 *
 * Module 3 Phase B 2026-04-20.  DRAFT — senior-reviewer (Opus) must approve
 * before php artisan migrate is run.
 *
 * Purpose
 * -------
 * Tracks every ingested version of a document.  Each time a file lands in
 * Bronze and is parsed into silver.reports, one row is appended here.
 * Re-ingesting the same file at a newer parser version creates revision_number=2
 * and sets superseded_by_revision_id on the old row — preserving the full audit
 * trail without mutating Bronze.
 *
 * FK graph (parent tables must already exist):
 *   document_revisions.document_id  → silver.reports.report_id   (CASCADE DELETE)
 *   document_revisions.workspace_id → silver.workspaces.workspace_id (CASCADE DELETE)
 *   document_revisions.superseded_by_revision_id
 *       → silver.document_revisions.document_revision_id  (self-FK, SET NULL)
 *
 * PK type decision
 * ----------------
 * UUID, matching silver.document_passages.passage_id and silver.reports.report_id.
 * All PKs in silver.* use UUID (gen_random_uuid()).  BIGINT IDENTITY is only
 * used in bronze.provenance-adjacent tables; silver tables are UUID throughout.
 *
 * ENUM vs CHECK
 * -------------
 * No evidence_type enum in this table.  No existing ENUM types found in any
 * migration (grep confirmed zero CREATE TYPE … AS ENUM in database/migrations/).
 * This table carries no type discriminator column — that lives in evidence_items.
 *
 * Rollback: drop table (all dependent tables are created in later migrations;
 * down() order is reversed at the migration level — structured_record_lineage
 * first, then evidence_items, then document_revisions).
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
        // Create silver.document_revisions
        // -----------------------------------------------------------------------
        DB::statement(
            'CREATE TABLE IF NOT EXISTS silver.document_revisions (
                document_revision_id        UUID        NOT NULL DEFAULT gen_random_uuid(),
                document_id                 UUID        NOT NULL
                    REFERENCES silver.reports(report_id) ON DELETE CASCADE,
                workspace_id                UUID        NOT NULL
                    REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                revision_number             INTEGER     NOT NULL,
                source_uri                  TEXT        NOT NULL,
                source_sha256               CHAR(64)    NOT NULL,
                ingested_at                 TIMESTAMPTZ NOT NULL,
                parser_name                 VARCHAR(128) NOT NULL,
                parser_version              VARCHAR(64)  NOT NULL,
                superseded_by_revision_id   UUID        NULL,
                created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                CONSTRAINT document_revisions_pkey
                    PRIMARY KEY (document_revision_id),

                -- A document can only have one row per revision number.
                CONSTRAINT document_revisions_unique_revision
                    UNIQUE (document_id, revision_number),

                -- sha256 must be 64 lowercase hex characters.
                CONSTRAINT document_revisions_sha256_format
                    CHECK (source_sha256 ~ \'^[0-9a-f]{64}$\'),

                -- revision_number starts at 1.
                CONSTRAINT document_revisions_revision_positive
                    CHECK (revision_number >= 1)
            )'
        );

        // -----------------------------------------------------------------------
        // Self-FK: superseded_by_revision_id points forward to the newer revision.
        // Added after table creation to avoid forward-reference at DDL parse time.
        // ON DELETE SET NULL: if the newer revision row is deleted, the pointer
        // becomes NULL rather than cascade-deleting the older (still valid) row.
        // -----------------------------------------------------------------------
        DB::statement(
            'ALTER TABLE silver.document_revisions
                ADD CONSTRAINT document_revisions_superseded_by_fkey
                    FOREIGN KEY (superseded_by_revision_id)
                    REFERENCES silver.document_revisions(document_revision_id)
                    ON DELETE SET NULL'
        );

        // -----------------------------------------------------------------------
        // Indices per §04j spec
        // -----------------------------------------------------------------------
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_doc_revisions_document_id
                 ON silver.document_revisions (document_id)'
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_doc_revisions_workspace_id
                 ON silver.document_revisions (workspace_id)'
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_doc_revisions_source_sha256
                 ON silver.document_revisions (source_sha256)'
        );
    }

    public function down(): void
    {
        // Drop self-FK first (needed before table drop on some PG versions,
        // though CASCADE handles it — explicit is safer in CI).
        DB::statement(
            'ALTER TABLE IF EXISTS silver.document_revisions
                DROP CONSTRAINT IF EXISTS document_revisions_superseded_by_fkey'
        );

        DB::statement('DROP TABLE IF EXISTS silver.document_revisions');
    }
};
