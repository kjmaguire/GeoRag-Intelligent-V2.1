<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create silver.ingest_layouts — per-region layout classification (Docling).
 *
 * Master-plan §3 / §9.3, doc-phase 50.
 *
 * Docling layout-first parsing produces per-region rows with the
 * region's semantic label (text, table, figure, header, footer, etc.)
 * which drives dispatch in parse_mixed (Step 5).
 *
 * Same §9.3 provenance contract as ingest_extractions; this table
 * adds the layout_label dimension.
 *
 * Naming: report_id (not pdf_id) — see ocr_page_quality migration.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('SET search_path TO silver, public;');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.ingest_layouts (
                report_id              UUID         NOT NULL,
                page                   INTEGER      NOT NULL,
                region                 INTEGER      NOT NULL,
                workspace_id           UUID         NOT NULL,
                bbox                   NUMERIC[]    NOT NULL,
                source_method          VARCHAR(40)  NOT NULL,
                extraction_confidence  NUMERIC(5,4) NULL,
                layout_label           VARCHAR(40)  NOT NULL,
                payload                JSONB        NOT NULL DEFAULT '{}'::jsonb,
                created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

                CONSTRAINT ingest_layouts_pkey
                    PRIMARY KEY (report_id, page, region),

                CONSTRAINT ingest_layouts_source_method_valid
                    CHECK (source_method IN (
                        'docling_layout_default',
                        'docling_layout_object_detection',
                        'docling_experimental_table_crops_layout'
                    )),

                CONSTRAINT ingest_layouts_label_valid
                    CHECK (layout_label IN (
                        'text',
                        'title',
                        'section_header',
                        'list_item',
                        'table',
                        'figure',
                        'caption',
                        'footnote',
                        'page_header',
                        'page_footer',
                        'formula',
                        'code',
                        'other'
                    )),

                CONSTRAINT ingest_layouts_confidence_bounded
                    CHECK (
                        extraction_confidence IS NULL
                        OR (extraction_confidence BETWEEN 0 AND 1)
                    ),

                CONSTRAINT ingest_layouts_bbox_shape
                    CHECK (array_length(bbox, 1) = 4),

                CONSTRAINT ingest_layouts_region_nonneg
                    CHECK (region >= 0),

                CONSTRAINT ingest_layouts_report_id_fkey
                    FOREIGN KEY (report_id)
                    REFERENCES silver.reports (report_id)
                    ON DELETE CASCADE,

                CONSTRAINT ingest_layouts_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE
            );
        SQL);

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_ingest_layouts_workspace
             ON silver.ingest_layouts (workspace_id);',
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_ingest_layouts_report_page
             ON silver.ingest_layouts (report_id, page);',
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_ingest_layouts_label
             ON silver.ingest_layouts (layout_label);',
        );
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.ingest_layouts;');
    }
};
