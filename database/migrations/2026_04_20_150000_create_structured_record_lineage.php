<?php

/**
 * B8.1 (part 3) — EVID: Create silver.structured_record_lineage per addendum §04j.
 *
 * Module 3 Phase B 2026-04-20.  DRAFT — senior-reviewer (Opus) must approve
 * before php artisan migrate is run.
 *
 * Purpose
 * -------
 * Row-level provenance for structured-record evidence items.  Each row traces a
 * single structured-record evidence item (a collar, sample interval, survey
 * station, etc.) back to the exact Bronze object, parser run, and Dagster
 * execution that produced it.  This is the audit layer that satisfies
 * "reprocessing always starts from MinIO" — native_locator gives the row
 * pointer needed to re-derive the Silver row from Bronze.
 *
 * Designed for structured_record evidence only.  Document-passage lineage is
 * captured by document_revisions + document_passages.  Graph-edge and
 * map-feature provenance are out of scope for this table.
 *
 * FK graph:
 *   structured_record_lineage.evidence_id
 *       → silver.evidence_items.evidence_id  (CASCADE DELETE)
 *
 * evidence_items (140000) must exist before this migration runs.
 *
 * Rollback: DROP TABLE — this is the first table dropped in rollback sequence
 * (reverse order: 150000 → 140000 → 130000 → 160000 is a no-op if skipped).
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
        // Create silver.structured_record_lineage
        // -----------------------------------------------------------------------
        DB::statement(
            'CREATE TABLE IF NOT EXISTS silver.structured_record_lineage (
                lineage_id          UUID         NOT NULL DEFAULT gen_random_uuid(),
                evidence_id         UUID         NOT NULL
                    REFERENCES silver.evidence_items(evidence_id) ON DELETE CASCADE,
                bronze_uri          TEXT         NOT NULL,
                bronze_sha256       CHAR(64)     NOT NULL,
                parser_name         VARCHAR(128) NOT NULL,
                parser_version      VARCHAR(64)  NOT NULL,
                ingestion_run_id    UUID         NOT NULL,
                native_locator      JSONB        NOT NULL,
                created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

                CONSTRAINT structured_record_lineage_pkey
                    PRIMARY KEY (lineage_id),

                -- sha256 must be 64 lowercase hex characters (same pattern as document_revisions).
                CONSTRAINT structured_record_lineage_sha256_format
                    CHECK (bronze_sha256 ~ \'^[0-9a-f]{64}$\')
            )',
        );

        // -----------------------------------------------------------------------
        // Indices per §04j spec
        // -----------------------------------------------------------------------
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_srl_evidence_id
                 ON silver.structured_record_lineage (evidence_id)',
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_srl_ingestion_run_id
                 ON silver.structured_record_lineage (ingestion_run_id)',
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_srl_bronze_sha256
                 ON silver.structured_record_lineage (bronze_sha256)',
        );
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.structured_record_lineage');
    }
};
