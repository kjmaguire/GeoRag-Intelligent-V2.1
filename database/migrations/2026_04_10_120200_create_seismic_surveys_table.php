<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create silver.seismic_surveys — one row per ingested SEG-Y file.
 *
 * Stores metadata extracted from the SEG-Y binary and textual headers only.
 * No trace data is stored in PostgreSQL — trace data lives in MinIO Bronze.
 *
 * The bbox (bounding polygon) column is added via PostGIS AddGeometryColumn and
 * is left NULL for Milestone 2 — trace-coordinate extraction required for bbox
 * computation is deferred to a later milestone.
 *
 * References Section 04e (Core Data Schemas) and Section 04d (SEG-Y format
 * support via segyio).
 */
return new class extends Migration
{
    /**
     * Run the migrations.
     */
    public function up(): void
    {
        // Create the table without the geometry column first
        DB::statement("
            CREATE TABLE IF NOT EXISTS silver.seismic_surveys (
                survey_id              UUID        PRIMARY KEY,
                project_id             UUID        NULL,
                survey_name            VARCHAR(255) NOT NULL,
                survey_type            VARCHAR(10)  NOT NULL
                    CHECK (survey_type IN ('2D', '3D')),
                num_traces             INT          NOT NULL,
                num_samples_per_trace  INT          NOT NULL,
                sample_interval_us     INT          NOT NULL,
                record_length_ms       FLOAT        NOT NULL,
                inline_min             INT          NULL,
                inline_max             INT          NULL,
                xline_min              INT          NULL,
                xline_max              INT          NULL,
                source_file            VARCHAR(255) NOT NULL,
                file_size_bytes        BIGINT       NOT NULL,
                segy_revision          VARCHAR(10)  NULL,
                header_text            TEXT         NULL,
                created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT fk_seismic_surveys_project
                    FOREIGN KEY (project_id)
                    REFERENCES silver.projects(project_id)
                    ON DELETE SET NULL
            )
        ");

        // Add PostGIS geometry column for bounding polygon (EPSG:4326)
        DB::statement(
            "SELECT AddGeometryColumn('silver', 'seismic_surveys', 'bbox', 4326, 'POLYGON', 2)"
        );

        // Indexes: survey_type for fast 2D/3D filtering; project_id for project scoping
        DB::statement("
            CREATE INDEX IF NOT EXISTS idx_seismic_surveys_type
                ON silver.seismic_surveys (survey_type)
        ");

        DB::statement("
            CREATE INDEX IF NOT EXISTS idx_seismic_surveys_project
                ON silver.seismic_surveys (project_id)
        ");
    }

    /**
     * Reverse the migrations.
     */
    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.seismic_surveys CASCADE');
    }
};
