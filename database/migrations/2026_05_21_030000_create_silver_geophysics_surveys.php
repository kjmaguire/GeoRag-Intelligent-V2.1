<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Phase 4 / Step 4.2 — silver.geophysics_surveys (broad geophysics metadata).
 *
 * General metadata schema covering all 7 survey types the plan calls out:
 * seismic / magnetic / gravity / radiometric / IP / EM / other. NOT a
 * replacement for ``silver.seismic_surveys`` — that table keeps the
 * SEG-Y-specific columns (trace counts, sample interval, inline/xline
 * bounds). ``silver.geophysics_surveys`` sits one level above and may
 * reference seismic_surveys rows via a future FK once their relationship
 * is locked.
 *
 * Columns (per the plan's Step 4.2 table):
 *   - survey_id              uuid, PK
 *   - survey_type            enum (CHECK constraint)
 *   - survey_name            text
 *   - contractor             text, nullable
 *   - acquisition_date       date, nullable
 *   - line_ids               text[], nullable
 *   - aoi_geom               geometry(Polygon, 4326)
 *   - crs_epsg               int, nullable
 *   - processing_notes       text, nullable
 *   - interpretation_pdf_id  uuid → bronze.source_files(id), nullable
 *   - anomaly_summary        text, nullable
 *   - project_id             uuid → silver.projects(project_id)
 *   - workspace_id           uuid → silver.workspaces(workspace_id) (tenancy)
 *   - created_at / updated_at
 *
 * Workspace tenancy is mandatory (matches every other silver.* table).
 *
 * Indexes:
 *   - GIST on aoi_geom for spatial filtering
 *   - btree on project_id + workspace_id for the usual scoping path
 *
 * SQLite — gated on Postgres (PostGIS-only geometry type).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.geophysics_surveys (
                survey_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id           uuid NOT NULL,
                project_id             uuid,
                survey_type            varchar(16) NOT NULL,
                survey_name            text NOT NULL,
                contractor             text,
                acquisition_date       date,
                line_ids               text[],
                aoi_geom               geometry(Polygon, 4326),
                crs_epsg               integer,
                processing_notes       text,
                interpretation_pdf_id  uuid,
                anomaly_summary        text,
                created_at             timestamptz NOT NULL DEFAULT now(),
                updated_at             timestamptz NOT NULL DEFAULT now(),

                CONSTRAINT chk_geophysics_surveys_type
                    CHECK (survey_type IN ('seismic', 'magnetic', 'gravity',
                                            'radiometric', 'IP', 'EM', 'other')),
                CONSTRAINT chk_geophysics_surveys_crs_epsg
                    CHECK (crs_epsg IS NULL OR (crs_epsg BETWEEN 1024 AND 32767)),

                CONSTRAINT fk_geophysics_surveys_workspace
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE,
                CONSTRAINT fk_geophysics_surveys_project
                    FOREIGN KEY (project_id)
                    REFERENCES silver.projects (project_id)
                    ON DELETE SET NULL,
                CONSTRAINT fk_geophysics_surveys_pdf
                    FOREIGN KEY (interpretation_pdf_id)
                    REFERENCES bronze.source_files (id)
                    ON DELETE SET NULL
            )
        SQL);

        DB::statement("COMMENT ON TABLE silver.geophysics_surveys IS 'Phase 4 / Step 4.2 — broad geophysics survey metadata covering all 7 survey types. Distinct from silver.seismic_surveys (SEG-Y-specific).'");
        DB::statement("COMMENT ON COLUMN silver.geophysics_surveys.survey_type IS 'seismic / magnetic / gravity / radiometric / IP / EM / other';");
        DB::statement("COMMENT ON COLUMN silver.geophysics_surveys.line_ids IS 'Survey line or station identifiers as a string array.';");
        DB::statement("COMMENT ON COLUMN silver.geophysics_surveys.aoi_geom IS 'Survey area polygon in EPSG:4326.';");
        DB::statement("COMMENT ON COLUMN silver.geophysics_surveys.interpretation_pdf_id IS 'FK to bronze.source_files — the interpretation report PDF.';");
        DB::statement("COMMENT ON COLUMN silver.geophysics_surveys.anomaly_summary IS 'Plain-text summary of key anomalies for the agentic-retrieval anomaly subgraph.';");

        DB::statement('CREATE INDEX IF NOT EXISTS idx_geophysics_surveys_aoi_gist ON silver.geophysics_surveys USING gist (aoi_geom)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geophysics_surveys_project ON silver.geophysics_surveys (project_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geophysics_surveys_workspace ON silver.geophysics_surveys (workspace_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geophysics_surveys_type ON silver.geophysics_surveys (survey_type)');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS silver.geophysics_surveys CASCADE');
    }
};
