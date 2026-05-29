<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * CC-03 Item 2 — silver.completeness_findings.
 *
 * Anna's explicit ask (2026-05-23). Distinct from the CC-01 Item 5
 * rule-based heading checklist (which only flags missing canonical
 * NI 43-101 sections). This is an audit pipeline that catches the
 * subtler "things mentioned but undocumented" gaps:
 *
 *   - work_types_undocumented        — text mentions "geophysical
 *                                       survey conducted" with no
 *                                       accompanying data file
 *   - coords_unmappable              — coordinate mentioned in text
 *                                       (silver.pdf_coordinates) with
 *                                       no matching feature in
 *                                       silver.collars / .spatial_features
 *   - qaqc_described_incomplete      — QA/QC section names blanks /
 *                                       CRMs / duplicates but the
 *                                       silver.assays_v2 batch lacks
 *                                       the corresponding rows
 *   - prior_recommendations_orphaned — earlier report (same project)
 *                                       has a Recommendations section
 *                                       with action items that don't
 *                                       appear as discussed in any
 *                                       later report
 *   - attachments_referenced_missing — text says "see Appendix B" but
 *                                       no Appendix B is in the
 *                                       uploaded file set
 *
 * Each finding row carries a severity (info/warn/error), a free-text
 * description, the source page that triggered it, and optional
 * jsonb evidence (e.g. {expected_file: "Appendix_B.pdf"}).
 *
 * Findings are workspace-scoped + pdf-scoped so re-running the audit
 * for the same PDF can DELETE the prior batch via finding_run_id.
 *
 * SQLite — gated on Postgres.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.completeness_findings (
                finding_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id      uuid NOT NULL,
                pdf_id            char(64) NOT NULL,
                project_id        uuid,
                finding_run_id    uuid NOT NULL,
                finding_kind      text NOT NULL,
                severity          text NOT NULL,
                description       text NOT NULL,
                source_page       integer,
                evidence          jsonb NOT NULL DEFAULT '{}'::jsonb,
                created_at        timestamptz NOT NULL DEFAULT now(),

                CONSTRAINT chk_completeness_kind
                    CHECK (finding_kind IN (
                        'work_types_undocumented',
                        'coords_unmappable',
                        'qaqc_described_incomplete',
                        'prior_recommendations_orphaned',
                        'attachments_referenced_missing'
                    )),
                CONSTRAINT chk_completeness_severity
                    CHECK (severity IN ('info', 'warn', 'error')),
                CONSTRAINT chk_completeness_pdf_id_hex
                    CHECK (pdf_id ~ '^[0-9a-f]{64}$'),
                CONSTRAINT chk_completeness_page
                    CHECK (source_page IS NULL OR source_page >= 1)
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS idx_completeness_workspace ON silver.completeness_findings (workspace_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_completeness_pdf_id ON silver.completeness_findings (pdf_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_completeness_run ON silver.completeness_findings (finding_run_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_completeness_kind ON silver.completeness_findings (finding_kind, severity)');

        DB::statement("COMMENT ON TABLE silver.completeness_findings IS
            'CC-03 Item 2 — post-ingestion completeness audit findings. Re-runnable per pdf_id; each run gets a new finding_run_id so the most-recent set can be filtered. Distinct from the CC-01 Item 5 heading-only checklist.'");
        DB::statement("COMMENT ON COLUMN silver.completeness_findings.finding_kind IS
            'work_types_undocumented | coords_unmappable | qaqc_described_incomplete | prior_recommendations_orphaned | attachments_referenced_missing';");
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS silver.completeness_findings CASCADE');
    }
};
