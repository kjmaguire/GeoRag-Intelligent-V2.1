<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Plan §1g — QA/QC schema (data quality flags).
 *
 * Flags are produced at ingestion time when validation rules detect a
 * potentially-bad value: assay outside 3σ, negative grade, missing CRS,
 * overlapping intervals, unit mid-hole change, etc.  Severity ERROR
 * blocks the document from entering 'ready' state; WARNING produces
 * 'ready_needs_review' (see plan §0g ingestion state machine).
 *
 * One row per (record_type, record_id, flag_type) detection event.
 * Resolution is recorded in-place — `resolved_at`/`reviewed_by`/
 * `resolution` are nullable until a human acts on the flag.
 *
 * NOTE — Kyle has an in-progress `silver.completeness_findings` table
 * (untracked on main as of 2026-05-26). That table tracks completeness
 * findings; this table tracks *correctness* validation flags. Adjacent
 * but not overlapping concerns. If consolidating into a single
 * findings table is preferred, this migration can be dropped before
 * merging the overnight branch.
 *
 * RLS: workspace-scoped. NOT applied by overnight run.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.data_quality_flags (
                flag_id            UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id       UUID         NOT NULL
                    REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                project_id         UUID         NULL,

                -- What the flag is about.
                record_type        VARCHAR(40)  NOT NULL,
                record_id          TEXT         NOT NULL,
                source_document_id UUID         NULL,
                source_page        INTEGER      NULL,
                source_row_range   TEXT         NULL,

                -- The flag itself.
                flag_type          VARCHAR(60)  NOT NULL,
                severity           VARCHAR(10)  NOT NULL,
                description        TEXT         NOT NULL,
                rule_id            VARCHAR(60)  NULL,
                rule_version       VARCHAR(20)  NULL,
                threshold_payload  JSONB        NOT NULL DEFAULT '{}'::jsonb,

                -- Detection + resolution lifecycle.
                flagged_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                flagged_by         VARCHAR(40)  NOT NULL DEFAULT 'system',
                reviewed_by_user_id BIGINT      NULL
                    REFERENCES public.users(id) ON DELETE SET NULL,
                reviewed_at        TIMESTAMPTZ  NULL,
                resolved_at        TIMESTAMPTZ  NULL,
                resolution         VARCHAR(40)  NULL,
                resolution_notes   TEXT         NULL,

                CONSTRAINT data_quality_flags_pkey PRIMARY KEY (flag_id),

                CONSTRAINT dqf_severity_valid
                    CHECK (severity IN ('INFO', 'WARNING', 'ERROR')),

                CONSTRAINT dqf_record_type_valid
                    CHECK (record_type IN (
                        'assay_interval', 'collar', 'survey_point',
                        'lithology_interval', 'alteration_interval',
                        'mineralization_interval', 'structural_interval',
                        'downhole_geophysics_point', 'composite_interval',
                        'document_chunk', 'table_extraction', 'spatial_feature',
                        'sample', 'geochronology_sample'
                    )),

                CONSTRAINT dqf_resolution_valid
                    CHECK (resolution IS NULL OR resolution IN (
                        'corrected', 'confirmed_valid', 'ignored',
                        'deferred', 'duplicate', 'rule_revised'
                    )),

                CONSTRAINT dqf_flagged_by_valid
                    CHECK (flagged_by IN ('system', 'user', 'sme_review'))
            )
        SQL);

        // Indexes — read paths from the plan §6a document view's QA/QC
        // badge and the §1g blocking-ready gate.
        DB::statement('CREATE INDEX IF NOT EXISTS idx_dqf_workspace_record
                       ON silver.data_quality_flags (workspace_id, record_type, record_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_dqf_workspace_severity
                       ON silver.data_quality_flags (workspace_id, severity, flagged_at DESC)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_dqf_source_document
                       ON silver.data_quality_flags (source_document_id)
                       WHERE source_document_id IS NOT NULL');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_dqf_open_errors
                       ON silver.data_quality_flags (workspace_id, flagged_at DESC)
                       WHERE severity = \'ERROR\' AND resolved_at IS NULL');

        // RLS — canonical workspace isolation.
        DB::statement('ALTER TABLE silver.data_quality_flags ENABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE silver.data_quality_flags FORCE ROW LEVEL SECURITY');
        DB::statement(<<<'SQL'
            CREATE POLICY data_quality_flags_workspace_isolation
                ON silver.data_quality_flags
                USING (
                    workspace_id::text = current_setting('georag.workspace_id', true)
                )
                WITH CHECK (
                    workspace_id::text = current_setting('georag.workspace_id', true)
                )
        SQL);

        DB::statement('GRANT SELECT, INSERT, UPDATE, DELETE
                       ON silver.data_quality_flags TO georag_app');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS silver.data_quality_flags CASCADE');
    }
};
