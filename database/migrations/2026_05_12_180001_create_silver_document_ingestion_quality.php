<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create silver.document_ingestion_quality — per-document quality summary.
 *
 * Master-plan §3 / §9.6, doc-phase 50.
 *
 * One row per silver.reports row; written at end of ingest by the Hatchet
 * ingest_pdf step. The recommended_action drives downstream Silver Review
 * routing.
 *
 * Naming: report_id (not pdf_id) — see ocr_page_quality migration.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('SET search_path TO silver, public;');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.document_ingestion_quality (
                report_id             UUID         NOT NULL,
                workspace_id          UUID         NOT NULL,
                total_pages           INTEGER      NOT NULL,
                low_confidence_pages  INTEGER      NOT NULL DEFAULT 0,
                table_pages           INTEGER      NOT NULL DEFAULT 0,
                map_pages             INTEGER      NOT NULL DEFAULT 0,
                overall_quality_score NUMERIC(5,4) NULL,
                recommended_action    VARCHAR(40)  NOT NULL DEFAULT 'accept',
                created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

                CONSTRAINT document_ingestion_quality_pkey
                    PRIMARY KEY (report_id),

                CONSTRAINT document_ingestion_quality_action_valid
                    CHECK (recommended_action IN (
                        'accept',
                        'accept_with_review',
                        'review_all_pages',
                        'reject'
                    )),

                CONSTRAINT document_ingestion_quality_pages_nonneg
                    CHECK (
                        total_pages > 0
                        AND low_confidence_pages >= 0
                        AND low_confidence_pages <= total_pages
                        AND table_pages >= 0
                        AND table_pages <= total_pages
                        AND map_pages >= 0
                        AND map_pages <= total_pages
                    ),

                CONSTRAINT document_ingestion_quality_score_bounded
                    CHECK (
                        overall_quality_score IS NULL
                        OR (overall_quality_score BETWEEN 0 AND 1)
                    ),

                CONSTRAINT document_ingestion_quality_report_id_fkey
                    FOREIGN KEY (report_id)
                    REFERENCES silver.reports (report_id)
                    ON DELETE CASCADE,

                CONSTRAINT document_ingestion_quality_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE
            );
        SQL);

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_document_ingestion_quality_workspace
             ON silver.document_ingestion_quality (workspace_id);',
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_document_ingestion_quality_action
             ON silver.document_ingestion_quality (recommended_action)
             WHERE recommended_action != \'accept\';',
        );
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.document_ingestion_quality;');
    }
};
