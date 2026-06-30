<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create silver.ocr_page_quality — per-page OCR + layout + table confidence.
 *
 * Master-plan §3 / §9.6, doc-phase 50.
 *
 * Naming note: master plan §9.6 uses `pdf_id`; this table uses `report_id`
 * for consistency with the existing silver.reports.report_id (canonical
 * silver-schema naming). Deliberate deviation — single canonical FK target
 * name across silver.*.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('SET search_path TO silver, public;');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.ocr_page_quality (
                report_id           UUID         NOT NULL,
                page                INTEGER      NOT NULL,
                workspace_id        UUID         NOT NULL,
                ocr_confidence      NUMERIC(5,4) NULL,
                layout_confidence   NUMERIC(5,4) NULL,
                table_confidence    NUMERIC(5,4) NULL,
                rotation_applied    NUMERIC(6,3) NULL,
                deskew_applied      BOOLEAN      NOT NULL DEFAULT FALSE,
                parser_used         VARCHAR(40)  NOT NULL,
                retry_count         INTEGER      NOT NULL DEFAULT 0,
                needs_review        BOOLEAN      NOT NULL DEFAULT FALSE,
                created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                last_evaluated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

                CONSTRAINT ocr_page_quality_pkey
                    PRIMARY KEY (report_id, page),

                CONSTRAINT ocr_page_quality_parser_valid
                    CHECK (parser_used IN (
                        'native',
                        'scanned_paddleocr',
                        'mixed_docling',
                        'table_heavy',
                        'map_heavy_unparsed'
                    )),

                CONSTRAINT ocr_page_quality_retry_nonneg
                    CHECK (retry_count >= 0 AND retry_count <= 5),

                CONSTRAINT ocr_page_quality_confidence_bounded
                    CHECK (
                        (ocr_confidence    IS NULL OR (ocr_confidence    BETWEEN 0 AND 1))
                        AND (layout_confidence IS NULL OR (layout_confidence BETWEEN 0 AND 1))
                        AND (table_confidence  IS NULL OR (table_confidence  BETWEEN 0 AND 1))
                    ),

                CONSTRAINT ocr_page_quality_report_id_fkey
                    FOREIGN KEY (report_id)
                    REFERENCES silver.reports (report_id)
                    ON DELETE CASCADE,

                CONSTRAINT ocr_page_quality_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE
            );
        SQL);

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_ocr_page_quality_workspace
             ON silver.ocr_page_quality (workspace_id);',
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_ocr_page_quality_needs_review
             ON silver.ocr_page_quality (needs_review) WHERE needs_review = TRUE;',
        );
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.ocr_page_quality;');
    }
};
