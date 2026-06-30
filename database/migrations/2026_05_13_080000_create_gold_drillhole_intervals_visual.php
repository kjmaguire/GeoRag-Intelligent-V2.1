<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create gold.drillhole_intervals_visual — pre-computed strip-log
 * visualization data.
 *
 * Master-plan §5 / §17 reference. One row per (collar_id, depth_from,
 * depth_to) interval. A Dagster asset (silver_lithology_logs +
 * silver_assays + silver_alterations → gold.drillhole_intervals_visual)
 * computes this table on each ingest batch.
 *
 * Doc-phase 71. Schema is intentionally JSONB-heavy to absorb future
 * §5 sub-step tuning without requiring schema migrations every time.
 *
 * Naming: collar_id (canonical silver.collars FK).
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('SET search_path TO gold, silver, public;');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS gold.drillhole_intervals_visual (
                visual_id          UUID         NOT NULL DEFAULT gen_random_uuid(),
                collar_id          UUID         NOT NULL,
                workspace_id       UUID         NOT NULL,
                project_id         UUID         NOT NULL,
                depth_from         NUMERIC(10,3) NOT NULL,
                depth_to           NUMERIC(10,3) NOT NULL,
                interval_kind      VARCHAR(40)  NOT NULL,
                lithology_code     VARCHAR(40)  NULL,
                lithology_label    TEXT         NULL,
                color_hint         VARCHAR(20)  NULL,
                assay_payload      JSONB        NOT NULL DEFAULT '{}'::jsonb,
                alteration_payload JSONB        NOT NULL DEFAULT '{}'::jsonb,
                structure_payload  JSONB        NOT NULL DEFAULT '{}'::jsonb,
                visual_y_start     NUMERIC(10,3) NULL,
                visual_y_end       NUMERIC(10,3) NULL,
                computed_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

                CONSTRAINT drillhole_intervals_visual_pkey
                    PRIMARY KEY (visual_id),

                CONSTRAINT drillhole_intervals_visual_collar_depth_unique
                    UNIQUE (collar_id, depth_from, depth_to, interval_kind),

                CONSTRAINT drillhole_intervals_visual_kind_valid
                    CHECK (interval_kind IN (
                        'lithology',
                        'alteration',
                        'structure',
                        'assay_high_grade',
                        'sample_window',
                        'other'
                    )),

                CONSTRAINT drillhole_intervals_visual_depth_valid
                    CHECK (depth_from >= 0 AND depth_to > depth_from),

                CONSTRAINT drillhole_intervals_visual_collar_id_fkey
                    FOREIGN KEY (collar_id)
                    REFERENCES silver.collars (collar_id)
                    ON DELETE CASCADE,

                CONSTRAINT drillhole_intervals_visual_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE
            );
        SQL);

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_drillhole_intervals_visual_collar
             ON gold.drillhole_intervals_visual (collar_id, depth_from);',
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_drillhole_intervals_visual_workspace
             ON gold.drillhole_intervals_visual (workspace_id);',
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_drillhole_intervals_visual_project
             ON gold.drillhole_intervals_visual (project_id);',
        );
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS gold.drillhole_intervals_visual;');
    }
};
