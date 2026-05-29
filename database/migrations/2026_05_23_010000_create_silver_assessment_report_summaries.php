<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * CC-01 Item 5 / Step 5.1 — silver.assessment_report_summaries.
 *
 * Stores the structured, source-cited summary of an ingested assessment
 * report (NI 43-101, JORC, internal company report, etc). Composes the
 * existing §04p Qwen-VL section summariser — this table is the durable
 * answer cache; the service in app/services/assessment_summarizer.py
 * does the orchestration.
 *
 * Keyed on pdf_id (SHA-256 of the normalised PDF bytes, the §04p Bronze
 * key). report_id is a nullable FK to silver.reports when the document
 * has been promoted to a full report record; the summary works without
 * it for any ingested PDF.
 *
 * Columns:
 *   - summary_id              uuid, PK
 *   - workspace_id            uuid → silver.workspaces, NOT NULL (tenancy)
 *   - pdf_id                  char(64) NOT NULL (the §04p Bronze key)
 *   - report_id               uuid → silver.reports, nullable
 *   - sections                jsonb NOT NULL — structured 9-section payload
 *                             (property_project, location, commodities,
 *                              operator, year, work_performed, qa_qc,
 *                              recommendations + extras the LLM finds)
 *   - completeness_checklist  jsonb NOT NULL — per-section "expected vs
 *                             found" flags from the rule-based v1 checker
 *   - mean_claim_confidence   real, nullable — mean of per-claim VL conf
 *   - model_id                text NOT NULL — VL model that produced it
 *   - model_backend           text NOT NULL — vllm | anthropic | ollama
 *   - generated_at            timestamptz NOT NULL DEFAULT now()
 *   - created_at / updated_at
 *
 * Each entry in `sections` carries `claims: [{text, page, bbox, confidence}]`
 * matching the VlClaim shape — citation is mandatory per §04i.
 *
 * Indexes:
 *   - UNIQUE (workspace_id, pdf_id, model_id) — one summary per workspace+pdf+model
 *   - btree on report_id when set
 *
 * SQLite — gated on Postgres (jsonb + uuid + FK to PostGIS workspaces table).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.assessment_report_summaries (
                summary_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id            uuid NOT NULL,
                pdf_id                  char(64) NOT NULL,
                report_id               uuid,
                sections                jsonb NOT NULL,
                completeness_checklist  jsonb NOT NULL,
                mean_claim_confidence   real,
                model_id                text NOT NULL,
                model_backend           text NOT NULL,
                generated_at            timestamptz NOT NULL DEFAULT now(),
                created_at              timestamptz NOT NULL DEFAULT now(),
                updated_at              timestamptz NOT NULL DEFAULT now(),

                CONSTRAINT chk_assessment_summary_pdf_id_hex
                    CHECK (pdf_id ~ '^[0-9a-f]{64}$'),
                CONSTRAINT chk_assessment_summary_confidence
                    CHECK (mean_claim_confidence IS NULL
                           OR (mean_claim_confidence >= 0 AND mean_claim_confidence <= 1)),
                CONSTRAINT chk_assessment_summary_backend
                    CHECK (model_backend IN ('vllm', 'anthropic', 'ollama')),

                CONSTRAINT fk_assessment_summary_workspace
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE,
                CONSTRAINT fk_assessment_summary_report
                    FOREIGN KEY (report_id)
                    REFERENCES silver.reports (report_id)
                    ON DELETE SET NULL
            )
        SQL);

        DB::statement(<<<'SQL'
            CREATE UNIQUE INDEX IF NOT EXISTS uq_assessment_summary_workspace_pdf_model
                ON silver.assessment_report_summaries (workspace_id, pdf_id, model_id)
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS idx_assessment_summary_report_id ON silver.assessment_report_summaries (report_id) WHERE report_id IS NOT NULL');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_assessment_summary_workspace ON silver.assessment_report_summaries (workspace_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_assessment_summary_pdf_id ON silver.assessment_report_summaries (pdf_id)');

        DB::statement("COMMENT ON TABLE silver.assessment_report_summaries IS
            'CC-01 Item 5 — structured, source-cited summary of an ingested assessment report. Composes /pdf/summarize_section output. Citation per §04i Citation completeness guard.'");
        DB::statement("COMMENT ON COLUMN silver.assessment_report_summaries.pdf_id IS
            'SHA-256 hex of the normalised PDF bytes — the §04p Bronze key.'");
        DB::statement("COMMENT ON COLUMN silver.assessment_report_summaries.sections IS
            'jsonb payload: {section_id: {title, summary_text, claims: [{text, page, bbox, confidence}]}}. The nine canonical sections + any extras the model surfaces.'");
        DB::statement("COMMENT ON COLUMN silver.assessment_report_summaries.completeness_checklist IS
            'jsonb payload: {expected_sections: [...], found: [...], missing: [...]}. Rule-based NI 43-101 §1–§27 coverage check.'");
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS silver.assessment_report_summaries CASCADE');
    }
};
