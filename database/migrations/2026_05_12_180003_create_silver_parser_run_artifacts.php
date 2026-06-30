<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create silver.parser_run_artifacts — per-parser-invocation audit trail.
 *
 * Master-plan §3 / §9.6, doc-phase 50.
 *
 * One row per parser invocation against a report. Used by Step 7
 * shadow comparison (parser_used = "p04p" vs "ragflow_shadow") and
 * by §9.8 XGBoost classifier training corpus (Phase 9).
 *
 * Naming: report_id (not pdf_id) — see ocr_page_quality migration.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('SET search_path TO silver, public;');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.parser_run_artifacts (
                run_id          UUID        NOT NULL DEFAULT gen_random_uuid(),
                report_id       UUID        NOT NULL,
                workspace_id    UUID        NOT NULL,
                parser_used     VARCHAR(40) NOT NULL,
                parser_version  VARCHAR(40) NOT NULL,
                raw_output_uri  TEXT        NULL,
                errors          JSONB       NOT NULL DEFAULT '[]'::jsonb,
                warnings        JSONB       NOT NULL DEFAULT '[]'::jsonb,
                started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finished_at     TIMESTAMPTZ NULL,

                CONSTRAINT parser_run_artifacts_pkey
                    PRIMARY KEY (run_id),

                CONSTRAINT parser_run_artifacts_parser_valid
                    CHECK (parser_used IN (
                        'native',
                        'scanned_paddleocr',
                        'mixed_docling',
                        'table_heavy',
                        'preflight',
                        'profiler',
                        'p04p',
                        'ragflow_shadow',
                        'v149_unstructured'
                    )),

                CONSTRAINT parser_run_artifacts_finished_after_started
                    CHECK (finished_at IS NULL OR finished_at >= started_at),

                CONSTRAINT parser_run_artifacts_report_id_fkey
                    FOREIGN KEY (report_id)
                    REFERENCES silver.reports (report_id)
                    ON DELETE CASCADE,

                CONSTRAINT parser_run_artifacts_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE
            );
        SQL);

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_parser_run_artifacts_report
             ON silver.parser_run_artifacts (report_id);',
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_parser_run_artifacts_workspace
             ON silver.parser_run_artifacts (workspace_id);',
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_parser_run_artifacts_parser
             ON silver.parser_run_artifacts (parser_used);',
        );
        // Partial index for Step 7 shadow comparison (only RAGFlow shadow rows).
        DB::statement(
            "CREATE INDEX IF NOT EXISTS idx_parser_run_artifacts_shadow
             ON silver.parser_run_artifacts (report_id)
             WHERE parser_used = 'ragflow_shadow';",
        );
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.parser_run_artifacts;');
    }
};
