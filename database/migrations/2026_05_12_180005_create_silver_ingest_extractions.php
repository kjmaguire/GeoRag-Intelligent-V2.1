<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create silver.ingest_extractions — per-region text extraction output.
 *
 * Master-plan §3 / §9.3, doc-phase 50.
 *
 * Per-region rows from native + mixed parser paths. The §9.3
 * provenance contract is (report_id, page, bbox, source_method,
 * extraction_confidence).
 *
 * Naming: report_id (not pdf_id) — see ocr_page_quality migration.
 * `region` is an integer ordinal assigned at parse time
 * (region 0 = first detected region on the page in reading order).
 *
 * bbox is stored as a numeric[4] (x0, y0, x1, y1) in PDF page coordinates.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('SET search_path TO silver, public;');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.ingest_extractions (
                report_id              UUID         NOT NULL,
                page                   INTEGER      NOT NULL,
                region                 INTEGER      NOT NULL,
                workspace_id           UUID         NOT NULL,
                bbox                   NUMERIC[]    NOT NULL,
                source_method          VARCHAR(40)  NOT NULL,
                extraction_confidence  NUMERIC(5,4) NULL,
                text_content           TEXT         NULL,
                payload                JSONB        NOT NULL DEFAULT '{}'::jsonb,
                created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

                CONSTRAINT ingest_extractions_pkey
                    PRIMARY KEY (report_id, page, region),

                CONSTRAINT ingest_extractions_source_method_valid
                    CHECK (source_method IN (
                        'pdfminer_six',
                        'pdfplumber_text',
                        'pdfplumber_table_cell',
                        'docling_text_region',
                        'docling_table_cell'
                    )),

                CONSTRAINT ingest_extractions_confidence_bounded
                    CHECK (
                        extraction_confidence IS NULL
                        OR (extraction_confidence BETWEEN 0 AND 1)
                    ),

                CONSTRAINT ingest_extractions_bbox_shape
                    CHECK (array_length(bbox, 1) = 4),

                CONSTRAINT ingest_extractions_region_nonneg
                    CHECK (region >= 0),

                CONSTRAINT ingest_extractions_report_id_fkey
                    FOREIGN KEY (report_id)
                    REFERENCES silver.reports (report_id)
                    ON DELETE CASCADE,

                CONSTRAINT ingest_extractions_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE
            );
        SQL);

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_ingest_extractions_workspace
             ON silver.ingest_extractions (workspace_id);'
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_ingest_extractions_report_page
             ON silver.ingest_extractions (report_id, page);'
        );
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.ingest_extractions;');
    }
};
