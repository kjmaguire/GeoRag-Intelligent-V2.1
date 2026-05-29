<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create gold.cross_section_panels — pre-projected cross-section data
 * for visualization.
 *
 * Master-plan §5 / §17 reference. One row per (project_id, section_name).
 * Stores the section line geometry + a JSONB array of collars projected
 * onto it (distance-along-line + depth + lithology sequence). A Dagster
 * asset computes this from silver.drill_traces + silver.lithology_logs.
 *
 * Doc-phase 71.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('SET search_path TO gold, silver, public;');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS gold.cross_section_panels (
                panel_id            UUID                            NOT NULL DEFAULT gen_random_uuid(),
                workspace_id        UUID                            NOT NULL,
                project_id          UUID                            NOT NULL,
                section_name        VARCHAR(120)                    NOT NULL,
                section_line_geom   GEOMETRY(LINESTRING, 4326)      NOT NULL,
                azimuth_deg         NUMERIC(6,3)                    NULL,
                length_m            NUMERIC(12,3)                   NULL,
                collars_projected   JSONB                           NOT NULL DEFAULT '[]'::jsonb,
                x_extent_m          NUMERIC(12,3)                   NULL,
                y_extent_m          NUMERIC(12,3)                   NULL,
                buffer_m            NUMERIC(10,3)                   NOT NULL DEFAULT 50,
                computed_at         TIMESTAMPTZ                     NOT NULL DEFAULT NOW(),
                created_at          TIMESTAMPTZ                     NOT NULL DEFAULT NOW(),

                CONSTRAINT cross_section_panels_pkey
                    PRIMARY KEY (panel_id),

                CONSTRAINT cross_section_panels_project_section_unique
                    UNIQUE (project_id, section_name),

                CONSTRAINT cross_section_panels_buffer_positive
                    CHECK (buffer_m > 0),

                CONSTRAINT cross_section_panels_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE
            );
        SQL);

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_cross_section_panels_geom
             ON gold.cross_section_panels USING GIST (section_line_geom);'
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_cross_section_panels_workspace
             ON gold.cross_section_panels (workspace_id);'
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_cross_section_panels_project
             ON gold.cross_section_panels (project_id);'
        );
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS gold.cross_section_panels;');
    }
};
