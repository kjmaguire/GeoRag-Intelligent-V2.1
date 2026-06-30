<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create silver.ingest_ocr_results — per-region OCR text + char confidences.
 *
 * Master-plan §3 / §9.3, doc-phase 50.
 *
 * Written by parse_scanned (PaddleOCR PP-OCRv5 image-input) and by
 * parse_mixed when it routes a region through scanned OCR. Char-level
 * confidence is preserved as JSONB for later quality analysis and
 * §9.8 XGBoost classifier training corpus.
 *
 * Naming: report_id (not pdf_id) — see ocr_page_quality migration.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('SET search_path TO silver, public;');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.ingest_ocr_results (
                report_id              UUID         NOT NULL,
                page                   INTEGER      NOT NULL,
                region                 INTEGER      NOT NULL,
                workspace_id           UUID         NOT NULL,
                bbox                   NUMERIC[]    NOT NULL,
                source_method          VARCHAR(40)  NOT NULL,
                extraction_confidence  NUMERIC(5,4) NULL,
                ocr_text               TEXT         NOT NULL,
                char_confidences       JSONB        NOT NULL DEFAULT '[]'::jsonb,
                language_hint          VARCHAR(10)  NULL,
                payload                JSONB        NOT NULL DEFAULT '{}'::jsonb,
                created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

                CONSTRAINT ingest_ocr_results_pkey
                    PRIMARY KEY (report_id, page, region),

                CONSTRAINT ingest_ocr_results_source_method_valid
                    CHECK (source_method IN (
                        'paddleocr_pp_ocrv5',
                        'paddleocr_pp_ocrv5_retry_binarized',
                        'paddleocr_pp_ocrv5_retry_lang_hint',
                        'paddleocr_pp_structure_v3_table_cell'
                    )),

                CONSTRAINT ingest_ocr_results_confidence_bounded
                    CHECK (
                        extraction_confidence IS NULL
                        OR (extraction_confidence BETWEEN 0 AND 1)
                    ),

                CONSTRAINT ingest_ocr_results_bbox_shape
                    CHECK (array_length(bbox, 1) = 4),

                CONSTRAINT ingest_ocr_results_region_nonneg
                    CHECK (region >= 0),

                CONSTRAINT ingest_ocr_results_report_id_fkey
                    FOREIGN KEY (report_id)
                    REFERENCES silver.reports (report_id)
                    ON DELETE CASCADE,

                CONSTRAINT ingest_ocr_results_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE
            );
        SQL);

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_ingest_ocr_results_workspace
             ON silver.ingest_ocr_results (workspace_id);',
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_ingest_ocr_results_report_page
             ON silver.ingest_ocr_results (report_id, page);',
        );
        // pg_trgm GIN index on ocr_text for fuzzy-search debugging during Step 4
        // tuning. Cheap to add now; useful when investigating which scanned
        // pages an OCR pass mis-recognized.
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_ingest_ocr_results_text_trgm
             ON silver.ingest_ocr_results USING GIN (ocr_text gin_trgm_ops);',
        );
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.ingest_ocr_results;');
    }
};
