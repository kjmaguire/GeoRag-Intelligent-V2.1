<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create silver.low_confidence_page_reviews — Silver Review queue rows.
 *
 * Master-plan §3 / §9.6, doc-phase 50.
 *
 * Populated by app.ocr.quality_graph.route_page() (Step 6 impl) when
 * a page lands below confidence threshold or hits the map-heavy v1
 * deferral. Drives the Silver Review UI (Step 8).
 *
 * Naming: report_id (not pdf_id) — see ocr_page_quality migration.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('SET search_path TO silver, public;');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.low_confidence_page_reviews (
                review_item_id    UUID        NOT NULL DEFAULT gen_random_uuid(),
                report_id         UUID        NOT NULL,
                page              INTEGER     NOT NULL,
                workspace_id      UUID        NOT NULL,
                reason            VARCHAR(60) NOT NULL,
                assigned_to       BIGINT      NULL,
                status            VARCHAR(20) NOT NULL DEFAULT 'pending',
                resolved_at       TIMESTAMPTZ NULL,
                resolution_notes  TEXT        NULL,
                created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                CONSTRAINT low_confidence_page_reviews_pkey
                    PRIMARY KEY (review_item_id),

                CONSTRAINT low_confidence_page_reviews_report_page_unique
                    UNIQUE (report_id, page, reason),

                CONSTRAINT low_confidence_page_reviews_reason_valid
                    CHECK (reason IN (
                        'ocr_confidence_below_threshold',
                        'layout_confidence_below_threshold',
                        'table_confidence_below_threshold',
                        'rotation_undetectable',
                        'deskew_failed_image_quality',
                        'page_blank_or_corrupted',
                        'map_heavy_v1_deferral',
                        'handwriting_unparseable',
                        'non_english_unsupported_language',
                        'encrypted_section',
                        'retry_max_exceeded',
                        'other'
                    )),

                CONSTRAINT low_confidence_page_reviews_status_valid
                    CHECK (status IN (
                        'pending',
                        'assigned',
                        'in_review',
                        'resolved_accept',
                        'resolved_reject',
                        'resolved_reocr_requested'
                    )),

                CONSTRAINT low_confidence_page_reviews_resolved_consistency
                    CHECK (
                        (status LIKE 'resolved_%' AND resolved_at IS NOT NULL)
                        OR (status NOT LIKE 'resolved_%' AND resolved_at IS NULL)
                    ),

                CONSTRAINT low_confidence_page_reviews_report_id_fkey
                    FOREIGN KEY (report_id)
                    REFERENCES silver.reports (report_id)
                    ON DELETE CASCADE,

                CONSTRAINT low_confidence_page_reviews_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE,

                CONSTRAINT low_confidence_page_reviews_assigned_to_fkey
                    FOREIGN KEY (assigned_to)
                    REFERENCES public.users (id)
                    ON DELETE SET NULL
            );
        SQL);

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_low_confidence_page_reviews_workspace_status
             ON silver.low_confidence_page_reviews (workspace_id, status);'
        );
        DB::statement(
            "CREATE INDEX IF NOT EXISTS idx_low_confidence_page_reviews_pending
             ON silver.low_confidence_page_reviews (created_at)
             WHERE status = 'pending';"
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_low_confidence_page_reviews_assigned_to
             ON silver.low_confidence_page_reviews (assigned_to)
             WHERE assigned_to IS NOT NULL;'
        );
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.low_confidence_page_reviews;');
    }
};
