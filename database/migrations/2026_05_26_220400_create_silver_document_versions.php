<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Plan §1h — document versioning and supersession.
 *
 * Problem: when a new NI 43-101 supersedes an older one, the old
 * document's chunks remain in Qdrant. Retrieval has no way to prefer
 * the current resource value over the superseded one. Same problem
 * for assessment reports, technical reports, fact sheets.
 *
 * Solution: track per-document version metadata so the retrieval
 * filter can default to `is_current = true` while still surfacing
 * superseded versions for explicit historical queries.
 *
 * The companion Qdrant payload field `is_current` (NOT touched in this
 * migration — Qdrant side is a separate write path) defaults to true
 * for newly-ingested chunks; supersession detection flips it to false
 * on the old document's chunks at the moment the new document lands
 * (see plan §1h supersession detection rules + the design doc).
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
            CREATE TABLE IF NOT EXISTS silver.document_versions (
                version_id         UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id       UUID         NOT NULL
                    REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                project_id         UUID         NULL,

                -- The document this is a version-of. FK to silver.reports;
                -- if the schema later splits documents from reports, this
                -- can become a polymorphic (document_kind, document_id).
                document_id        UUID         NOT NULL
                    REFERENCES silver.reports(report_id) ON DELETE CASCADE,

                -- Which document (if any) replaced this one.
                superseded_by_id   UUID         NULL
                    REFERENCES silver.reports(report_id) ON DELETE SET NULL,

                -- Version identity.
                report_type        VARCHAR(60)  NOT NULL,
                version_number     INTEGER      NOT NULL DEFAULT 1,
                effective_date     DATE         NULL,
                effective_date_source VARCHAR(40) NULL,

                -- Currency flag — denormalised for the retrieval filter
                -- fast path. Default true; supersession detection flips
                -- the old row to false at ingestion of a newer version.
                is_current         BOOLEAN      NOT NULL DEFAULT true,

                -- Property scope so supersession detection matches
                -- correctly (a 2024 NI 43-101 for Project A does not
                -- supersede a 2023 NI 43-101 for Project B).
                property_id        UUID         NULL,
                property_name      VARCHAR(255) NULL,

                -- Supersession audit trail.
                superseded_at      TIMESTAMPTZ  NULL,
                supersession_reason VARCHAR(255) NULL,
                superseded_by_event VARCHAR(40) NULL,

                created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

                CONSTRAINT document_versions_pkey PRIMARY KEY (version_id),

                -- One version row per document. Multiple rows for the
                -- same document indicate a re-classification, not
                -- multiple effective dates simultaneously.
                CONSTRAINT document_versions_document_unique
                    UNIQUE (document_id),

                CONSTRAINT document_versions_effective_date_source_valid
                    CHECK (effective_date_source IS NULL OR effective_date_source IN (
                        'document_text_extracted', 'filename_pattern',
                        'user_assigned', 'filing_date_fallback', 'unknown'
                    )),

                CONSTRAINT document_versions_event_valid
                    CHECK (superseded_by_event IS NULL OR superseded_by_event IN (
                        'auto_detected_on_ingest', 'user_marked', 'sme_review'
                    )),

                -- A row marked superseded MUST have superseded_by_id +
                -- superseded_at; the reverse (is_current=true) MUST NOT.
                CONSTRAINT document_versions_supersession_consistent
                    CHECK (
                        (is_current = true  AND superseded_by_id IS NULL AND superseded_at IS NULL)
                        OR
                        (is_current = false AND superseded_by_id IS NOT NULL AND superseded_at IS NOT NULL)
                    )
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS idx_doc_versions_workspace_current
                       ON silver.document_versions (workspace_id, is_current, report_type)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_doc_versions_property
                       ON silver.document_versions (workspace_id, property_id, report_type)
                       WHERE property_id IS NOT NULL');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_doc_versions_superseded_by
                       ON silver.document_versions (superseded_by_id)
                       WHERE superseded_by_id IS NOT NULL');

        DB::statement('ALTER TABLE silver.document_versions ENABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE silver.document_versions FORCE ROW LEVEL SECURITY');
        DB::statement(<<<'SQL'
            CREATE POLICY document_versions_workspace_isolation
                ON silver.document_versions
                USING (
                    workspace_id::text = current_setting('georag.workspace_id', true)
                )
                WITH CHECK (
                    workspace_id::text = current_setting('georag.workspace_id', true)
                )
        SQL);

        DB::statement('GRANT SELECT, INSERT, UPDATE, DELETE
                       ON silver.document_versions TO georag_app');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS silver.document_versions CASCADE');
    }
};
