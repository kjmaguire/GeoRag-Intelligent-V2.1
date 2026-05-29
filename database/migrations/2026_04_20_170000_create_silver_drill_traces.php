<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create silver.drill_traces — pre-computed minimum-curvature desurvey traces.
 *
 * Each row is a LINESTRINGZ in EPSG:4326 representing the 3-D path of one
 * drill hole, computed by the silver_drill_traces Dagster asset from the
 * surveys in silver.surveys.
 *
 * Design notes:
 *   - One trace per collar (UNIQUE on collar_id).
 *   - survey_hash (SHA-256 of sorted surveys) enables idempotent recompute:
 *     the asset skips recomputation when the hash matches the stored value.
 *   - trace_quality flags degenerate cases without blocking ingestion.
 *   - dogleg_max_deg records the highest dogleg severity for QA metadata.
 *   - workspace_id / project_id are denormalised for efficient spatial queries
 *     without repeated JOINs through collars → projects → workspaces.
 *
 * FK targets (verified against live DB 2026-04-20):
 *   collar_id   → silver.collars(collar_id)     ON DELETE CASCADE
 *   workspace_id → silver.workspaces(workspace_id) ON DELETE CASCADE
 *   project_id   → silver.projects(project_id)   ON DELETE CASCADE
 *
 * Chunk 2 — Module 3 Phase B (B5 drill_traces wiring).
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('SET search_path TO silver, public;');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.drill_traces (
                trace_id            UUID        NOT NULL DEFAULT gen_random_uuid(),
                collar_id           UUID        NOT NULL,
                workspace_id        UUID        NOT NULL,
                project_id          UUID        NOT NULL,
                geom                GEOMETRY(LINESTRINGZ, 4326) NOT NULL,
                computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                survey_hash         CHAR(64)    NOT NULL,
                dogleg_max_deg      NUMERIC(6,3) NULL,
                trace_quality       VARCHAR(32) NOT NULL DEFAULT 'ok',
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                CONSTRAINT drill_traces_pkey
                    PRIMARY KEY (trace_id),

                CONSTRAINT drill_traces_collar_unique
                    UNIQUE (collar_id),

                CONSTRAINT drill_traces_quality_valid
                    CHECK (trace_quality IN (
                        'ok',
                        'high_dogleg_warning',
                        'single_survey_vertical'
                    )),

                CONSTRAINT drill_traces_collar_id_fkey
                    FOREIGN KEY (collar_id)
                    REFERENCES silver.collars (collar_id)
                    ON DELETE CASCADE,

                CONSTRAINT drill_traces_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE,

                CONSTRAINT drill_traces_project_id_fkey
                    FOREIGN KEY (project_id)
                    REFERENCES silver.projects (project_id)
                    ON DELETE CASCADE
            );
        SQL);

        // GIST index for spatial queries (Martin tile consumption, Module 8).
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_drill_traces_geom
             ON silver.drill_traces USING GIST (geom);'
        );

        // Supporting indices for filtering by project / workspace / hash.
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_drill_traces_project
             ON silver.drill_traces (project_id);'
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_drill_traces_workspace
             ON silver.drill_traces (workspace_id);'
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_drill_traces_survey_hash
             ON silver.drill_traces (survey_hash);'
        );
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.drill_traces CASCADE;');
    }
};
