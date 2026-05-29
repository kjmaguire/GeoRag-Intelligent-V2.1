<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create silver.table_extraction_quality — per-table extraction confidence.
 *
 * Master-plan §3 / §9.6, doc-phase 50.
 *
 * One row per detected table per page. Table-heavy parser path
 * (Step 5) writes structure + cell confidence; low-confidence tables
 * trigger Silver Review routing.
 *
 * Naming: report_id (not pdf_id) — see ocr_page_quality migration.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('SET search_path TO silver, public;');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.table_extraction_quality (
                report_id            UUID         NOT NULL,
                page                 INTEGER      NOT NULL,
                table_id             INTEGER      NOT NULL,
                workspace_id         UUID         NOT NULL,
                structure_confidence NUMERIC(5,4) NULL,
                cell_confidence      NUMERIC(5,4) NULL,
                header_detected      BOOLEAN      NOT NULL DEFAULT FALSE,
                parser_used          VARCHAR(40)  NOT NULL,
                needs_review         BOOLEAN      NOT NULL DEFAULT FALSE,
                created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

                CONSTRAINT table_extraction_quality_pkey
                    PRIMARY KEY (report_id, page, table_id),

                CONSTRAINT table_extraction_quality_parser_valid
                    CHECK (parser_used IN (
                        'pdfplumber',
                        'docling_tableformer',
                        'docling_tableformer_v2',
                        'paddleocr_pp_structure_v3'
                    )),

                CONSTRAINT table_extraction_quality_confidence_bounded
                    CHECK (
                        (structure_confidence IS NULL OR (structure_confidence BETWEEN 0 AND 1))
                        AND (cell_confidence  IS NULL OR (cell_confidence  BETWEEN 0 AND 1))
                    ),

                CONSTRAINT table_extraction_quality_table_id_nonneg
                    CHECK (table_id >= 0),

                CONSTRAINT table_extraction_quality_report_id_fkey
                    FOREIGN KEY (report_id)
                    REFERENCES silver.reports (report_id)
                    ON DELETE CASCADE,

                CONSTRAINT table_extraction_quality_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE
            );
        SQL);

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_table_extraction_quality_workspace
             ON silver.table_extraction_quality (workspace_id);'
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_table_extraction_quality_needs_review
             ON silver.table_extraction_quality (needs_review) WHERE needs_review = TRUE;'
        );
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.table_extraction_quality;');
    }
};
