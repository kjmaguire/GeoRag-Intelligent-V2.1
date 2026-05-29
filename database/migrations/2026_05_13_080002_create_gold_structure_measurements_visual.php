<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create gold.structure_measurements_visual — pre-projected structure
 * measurements for stereonet plotting.
 *
 * Master-plan §5 / §17 reference. One row per (collar_id, depth)
 * structure measurement. Joins silver.structures + silver.collars +
 * silver.drill_traces (for the desurvey-corrected orientation if needed).
 * Pre-computes stereonet x/y coords + classification so the visualization
 * endpoint can render without re-running the geometry.
 *
 * Doc-phase 71.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('SET search_path TO gold, silver, public;');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS gold.structure_measurements_visual (
                visual_id           UUID         NOT NULL DEFAULT gen_random_uuid(),
                collar_id           UUID         NOT NULL,
                workspace_id        UUID         NOT NULL,
                project_id          UUID         NOT NULL,
                depth               NUMERIC(10,3) NOT NULL,
                structure_type      VARCHAR(40)  NOT NULL,
                strike_deg          NUMERIC(6,3) NULL,
                dip_deg             NUMERIC(6,3) NULL,
                dip_direction_deg   NUMERIC(6,3) NULL,
                plunge_deg          NUMERIC(6,3) NULL,
                trend_deg           NUMERIC(6,3) NULL,
                stereonet_x         NUMERIC(10,6) NULL,
                stereonet_y         NUMERIC(10,6) NULL,
                projection          VARCHAR(20)  NOT NULL DEFAULT 'equal_area',
                computed_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

                CONSTRAINT structure_measurements_visual_pkey
                    PRIMARY KEY (visual_id),

                CONSTRAINT structure_measurements_visual_type_valid
                    CHECK (structure_type IN (
                        'fault',
                        'shear',
                        'fracture',
                        'joint',
                        'vein',
                        'foliation',
                        'cleavage',
                        'bedding',
                        'contact',
                        'fold_axis',
                        'lineation',
                        'other'
                    )),

                CONSTRAINT structure_measurements_visual_projection_valid
                    CHECK (projection IN ('equal_area', 'equal_angle')),

                CONSTRAINT structure_measurements_visual_orientations_bounded
                    CHECK (
                        (strike_deg IS NULL OR strike_deg BETWEEN 0 AND 360)
                        AND (dip_deg IS NULL OR dip_deg BETWEEN 0 AND 90)
                        AND (dip_direction_deg IS NULL OR dip_direction_deg BETWEEN 0 AND 360)
                        AND (plunge_deg IS NULL OR plunge_deg BETWEEN 0 AND 90)
                        AND (trend_deg IS NULL OR trend_deg BETWEEN 0 AND 360)
                    ),

                CONSTRAINT structure_measurements_visual_depth_valid
                    CHECK (depth >= 0),

                CONSTRAINT structure_measurements_visual_collar_id_fkey
                    FOREIGN KEY (collar_id)
                    REFERENCES silver.collars (collar_id)
                    ON DELETE CASCADE,

                CONSTRAINT structure_measurements_visual_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE
            );
        SQL);

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_structure_measurements_visual_collar
             ON gold.structure_measurements_visual (collar_id, depth);'
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_structure_measurements_visual_workspace
             ON gold.structure_measurements_visual (workspace_id);'
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_structure_measurements_visual_project_type
             ON gold.structure_measurements_visual (project_id, structure_type);'
        );
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS gold.structure_measurements_visual;');
    }
};
