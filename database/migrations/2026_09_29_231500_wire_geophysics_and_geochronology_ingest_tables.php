<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * ING-19 (Kyle, 2026-09-29: "wire up") — give the three idle parsers a home.
 *
 * `xyz_parser`, `dcip2d_parser`/`dcip2d_survey` and `csv_geochronology` had
 * no caller: a Geosoft XYZ upload answered 422, and a DC/IP export or a
 * radiometric-age table inside a ZIP fell to `unknown` or to the text
 * fallback. The two parent tables already existed —
 * `silver.geophysics_surveys` (2026-05-21) and `silver.geochronology_samples`
 * (2026-05-24) — but held survey METADATA and one age per sample only, with
 * no lineage columns and uniqueness keys that would have made an ingest
 * either duplicate or cross-wire projects. This migration adds the minimum
 * the ingest path needs and nothing more.
 *
 * 1. silver.geophysics_surveys
 *    - Lineage on the row (source_file / source_file_sha256 /
 *      source_object_key / parser_name / parser_version), the same
 *      "lineage on the silver row" pattern silver.spatial_features uses,
 *      plus georef_method / crs_confidence with the vocabulary
 *      silver.collars and silver.geochronology_samples already use.
 *    - UNIQUE (workspace_id, survey_name) becomes UNIQUE NULLS NOT DISTINCT
 *      (workspace_id, project_id, survey_name). The old key was the upsert
 *      key of the deleted Dagster writer; with it, two projects in one
 *      workspace that each uploaded `mag.xyz` would have had the second
 *      upsert take over the first project's survey. Per project, a
 *      re-upload of the same file name replaces its own survey in place.
 *
 * 2. silver.geochronology_samples
 *    - The same lineage columns plus source_row, and the coordinates as
 *      given (x_native / y_native / source_epsg): geom is 4326, and a table
 *      located in UTM must keep the numbers it was delivered with.
 *    - UNIQUE (workspace_id, sample_id, isotopic_system) becomes UNIQUE
 *      NULLS NOT DISTINCT (workspace_id, project_id, sample_id,
 *      isotopic_system, mineral_dated). One sample routinely carries a
 *      zircon AND a titanite U-Pb age; the old key refused the second, and
 *      it was workspace-wide, so sample "SK-01" in one project blocked
 *      "SK-01" in another.
 *
 * 3. Four new tables for the measurements themselves (the parents hold
 *    metadata only):
 *    - silver.geophysics_lines        one row per XYZ line (or segment of a
 *                                     very long line): geometry in 4326,
 *                                     coordinates as given, source rows.
 *    - silver.geophysics_line_channels one row per (line, channel): the
 *                                     channel's values as an array, the
 *                                     shape silver.well_log_curves uses —
 *                                     an airborne line is 10^4-10^5 points
 *                                     x dozens of channels.
 *    - silver.geophysics_dcip_observations one row per DC/IP reading: four
 *                                     electrode CHAINAGES and a value. NO
 *                                     geometry column, deliberately: the
 *                                     chainage is a 1-D position along a
 *                                     line whose ground position the export
 *                                     does not carry (see dcip2d_survey).
 *    - silver.geophysics_dcip_models  one row per inversion model: nx x nz
 *                                     cells row-major surface-first, and the
 *                                     air mask. Mesh coordinates only.
 *
 *    Each carries workspace_id + project_id (ON DELETE CASCADE from the
 *    project and from its survey, so deleting a project or re-uploading a
 *    survey leaves nothing behind), ENABLE + FORCE ROW LEVEL SECURITY and a
 *    fail-closed `tenant_isolation` policy (the NULLIF(..., '') shape
 *    WorkspaceRlsCoverageTest locks), a leading-workspace_id index, and —
 *    where there is geometry — a GIST index on the 4326 column.
 *
 * Idempotent: IF NOT EXISTS throughout; constraint swaps are guarded.
 */
return new class extends Migration
{
    /** @var list<string> */
    private const NEW_TABLES = [
        'silver.geophysics_lines',
        'silver.geophysics_line_channels',
        'silver.geophysics_dcip_observations',
        'silver.geophysics_dcip_models',
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // ── 1. silver.geophysics_surveys ─────────────────────────────────
        DB::statement(<<<'SQL'
            ALTER TABLE silver.geophysics_surveys
                ADD COLUMN IF NOT EXISTS source_file        text,
                ADD COLUMN IF NOT EXISTS source_file_sha256 text,
                ADD COLUMN IF NOT EXISTS source_object_key  text,
                ADD COLUMN IF NOT EXISTS parser_name        text,
                ADD COLUMN IF NOT EXISTS parser_version     text,
                ADD COLUMN IF NOT EXISTS georef_method      varchar(16),
                ADD COLUMN IF NOT EXISTS crs_confidence     real
        SQL);
        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'chk_geophysics_surveys_georef_method'
                ) THEN
                    ALTER TABLE silver.geophysics_surveys
                        ADD CONSTRAINT chk_geophysics_surveys_georef_method
                        CHECK (georef_method IS NULL
                               OR georef_method IN ('declared', 'detected', 'assumed',
                                                    'manual', 'survey'));
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'chk_geophysics_surveys_crs_confidence'
                ) THEN
                    ALTER TABLE silver.geophysics_surveys
                        ADD CONSTRAINT chk_geophysics_surveys_crs_confidence
                        CHECK (crs_confidence IS NULL
                               OR (crs_confidence >= 0 AND crs_confidence <= 1));
                END IF;
                ALTER TABLE silver.geophysics_surveys
                    DROP CONSTRAINT IF EXISTS uq_geophysics_surveys_workspace_name;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'uq_geophysics_surveys_project_name'
                ) THEN
                    ALTER TABLE silver.geophysics_surveys
                        ADD CONSTRAINT uq_geophysics_surveys_project_name
                        UNIQUE NULLS NOT DISTINCT (workspace_id, project_id, survey_name);
                END IF;
            END $$;
        SQL);
        DB::statement("COMMENT ON CONSTRAINT uq_geophysics_surveys_project_name ON silver.geophysics_surveys IS
            'ING-19 (2026-09-29): the ingest_geophysics upsert key. Per project, so the same file name in two projects is two surveys; a re-upload in one project replaces its own survey.'");
        DB::statement("COMMENT ON COLUMN silver.geophysics_surveys.source_object_key IS
            'Bronze object key of the upload (or ZIP-extracted bundle) this survey was ingested from.'");

        // ── 2. silver.geochronology_samples ──────────────────────────────
        DB::statement(<<<'SQL'
            ALTER TABLE silver.geochronology_samples
                ADD COLUMN IF NOT EXISTS source_file        text,
                ADD COLUMN IF NOT EXISTS source_file_sha256 text,
                ADD COLUMN IF NOT EXISTS source_object_key  text,
                ADD COLUMN IF NOT EXISTS source_row         integer,
                ADD COLUMN IF NOT EXISTS parser_name        text,
                ADD COLUMN IF NOT EXISTS parser_version     text,
                ADD COLUMN IF NOT EXISTS x_native           double precision,
                ADD COLUMN IF NOT EXISTS y_native           double precision,
                ADD COLUMN IF NOT EXISTS source_epsg        integer
        SQL);
        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'chk_geochron_source_epsg'
                ) THEN
                    ALTER TABLE silver.geochronology_samples
                        ADD CONSTRAINT chk_geochron_source_epsg
                        CHECK (source_epsg IS NULL OR (source_epsg BETWEEN 1024 AND 32767));
                END IF;
                ALTER TABLE silver.geochronology_samples
                    DROP CONSTRAINT IF EXISTS uq_geochron_workspace_sample;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'uq_geochron_project_sample'
                ) THEN
                    ALTER TABLE silver.geochronology_samples
                        ADD CONSTRAINT uq_geochron_project_sample
                        UNIQUE NULLS NOT DISTINCT
                        (workspace_id, project_id, sample_id, isotopic_system, mineral_dated);
                END IF;
            END $$;
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geochron_project_source ON silver.geochronology_samples (project_id, source_file)');
        DB::statement("COMMENT ON CONSTRAINT uq_geochron_project_sample ON silver.geochronology_samples IS
            'ING-19 (2026-09-29): one age per (project, sample, isotopic system, mineral). Replaces the workspace-wide (sample, system) key, which refused a second mineral dated on the same sample.'");
        DB::statement("COMMENT ON COLUMN silver.geochronology_samples.source_epsg IS
            'EPSG the delivered x_native/y_native are in (4326 for a lon/lat table). geom is always 4326.'");

        // ── 3a. silver.geophysics_lines ──────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.geophysics_lines (
                line_pk        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id   uuid NOT NULL,
                project_id     uuid NOT NULL,
                survey_id      uuid NOT NULL,
                line_id        text,
                line_type      varchar(8) NOT NULL,
                segment        integer NOT NULL DEFAULT 1,
                point_count    integer NOT NULL,
                x_native       double precision[] NOT NULL,
                y_native       double precision[] NOT NULL,
                source_rows    integer[] NOT NULL,
                source_epsg    integer,
                geom           geometry(Geometry, 4326),
                created_at     timestamptz NOT NULL DEFAULT now(),

                CONSTRAINT chk_geophysics_lines_type
                    CHECK (line_type IN ('line', 'tie', 'trend', 'points')),
                CONSTRAINT chk_geophysics_lines_points
                    CHECK (point_count > 0 AND segment > 0),
                CONSTRAINT chk_geophysics_lines_source_epsg
                    CHECK (source_epsg IS NULL OR (source_epsg BETWEEN 1024 AND 32767)),
                CONSTRAINT chk_geophysics_lines_geom_type
                    CHECK (geom IS NULL
                           OR GeometryType(geom) IN ('LINESTRING', 'MULTIPOINT', 'POINT')),
                CONSTRAINT uq_geophysics_lines_segment
                    UNIQUE NULLS NOT DISTINCT (survey_id, line_id, segment),

                CONSTRAINT fk_geophysics_lines_workspace
                    FOREIGN KEY (workspace_id) REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE,
                CONSTRAINT fk_geophysics_lines_project
                    FOREIGN KEY (project_id) REFERENCES silver.projects (project_id)
                    ON DELETE CASCADE,
                CONSTRAINT fk_geophysics_lines_survey
                    FOREIGN KEY (survey_id) REFERENCES silver.geophysics_surveys (survey_id)
                    ON DELETE CASCADE
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geophysics_lines_geom_gist ON silver.geophysics_lines USING gist (geom)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geophysics_lines_workspace ON silver.geophysics_lines (workspace_id, project_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geophysics_lines_survey ON silver.geophysics_lines (survey_id)');
        DB::statement("COMMENT ON TABLE silver.geophysics_lines IS
            'ING-19 (2026-09-29): one row per line (or 100k-point segment) of a Geosoft XYZ survey. geom is the line in EPSG:4326 (MULTIPOINT for an unlined point set); x_native/y_native are the delivered coordinates in source_epsg; source_rows are 1-based file lines. Channel values live in silver.geophysics_line_channels.'");

        // ── 3b. silver.geophysics_line_channels ──────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.geophysics_line_channels (
                channel_pk     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id   uuid NOT NULL,
                project_id     uuid NOT NULL,
                survey_id      uuid NOT NULL,
                line_pk        uuid NOT NULL,
                channel_name   text NOT NULL,
                channel_values double precision[] NOT NULL,
                null_count     integer NOT NULL DEFAULT 0,
                min_value      double precision,
                max_value      double precision,
                created_at     timestamptz NOT NULL DEFAULT now(),

                CONSTRAINT uq_geophysics_line_channels_name UNIQUE (line_pk, channel_name),

                CONSTRAINT fk_geophysics_line_channels_workspace
                    FOREIGN KEY (workspace_id) REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE,
                CONSTRAINT fk_geophysics_line_channels_project
                    FOREIGN KEY (project_id) REFERENCES silver.projects (project_id)
                    ON DELETE CASCADE,
                CONSTRAINT fk_geophysics_line_channels_survey
                    FOREIGN KEY (survey_id) REFERENCES silver.geophysics_surveys (survey_id)
                    ON DELETE CASCADE,
                CONSTRAINT fk_geophysics_line_channels_line
                    FOREIGN KEY (line_pk) REFERENCES silver.geophysics_lines (line_pk)
                    ON DELETE CASCADE
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geophysics_line_channels_workspace ON silver.geophysics_line_channels (workspace_id, project_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geophysics_line_channels_survey ON silver.geophysics_line_channels (survey_id, channel_name)');
        DB::statement("COMMENT ON TABLE silver.geophysics_line_channels IS
            'ING-19 (2026-09-29): one row per (XYZ line, channel). values is parallel to geophysics_lines.x_native; NULL elements are Geosoft dummies (* or <= -1e31). No unit: an XYZ file carries none.'");

        // ── 3c. silver.geophysics_dcip_observations ──────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.geophysics_dcip_observations (
                observation_pk uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id   uuid NOT NULL,
                project_id     uuid NOT NULL,
                survey_id      uuid NOT NULL,
                line_id        text NOT NULL,
                source_file    text NOT NULL,
                source_row     integer NOT NULL,
                array_type     text NOT NULL,
                quantity       text NOT NULL,
                c1_chainage_m  double precision NOT NULL,
                c2_chainage_m  double precision NOT NULL,
                p1_chainage_m  double precision NOT NULL,
                p2_chainage_m  double precision NOT NULL,
                value          double precision NOT NULL,
                created_at     timestamptz NOT NULL DEFAULT now(),

                CONSTRAINT uq_geophysics_dcip_observations_row
                    UNIQUE (survey_id, source_file, source_row),

                CONSTRAINT fk_geophysics_dcip_obs_workspace
                    FOREIGN KEY (workspace_id) REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE,
                CONSTRAINT fk_geophysics_dcip_obs_project
                    FOREIGN KEY (project_id) REFERENCES silver.projects (project_id)
                    ON DELETE CASCADE,
                CONSTRAINT fk_geophysics_dcip_obs_survey
                    FOREIGN KEY (survey_id) REFERENCES silver.geophysics_surveys (survey_id)
                    ON DELETE CASCADE
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geophysics_dcip_obs_workspace ON silver.geophysics_dcip_observations (workspace_id, project_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geophysics_dcip_obs_survey ON silver.geophysics_dcip_observations (survey_id)');
        DB::statement("COMMENT ON TABLE silver.geophysics_dcip_observations IS
            'ING-19 (2026-09-29): one row per UBC-GIF DCIP2D observed reading. C1/C2/P1/P2 are electrode CHAINAGES along the line (metres), NOT x/y/z — see georag_geoparsers.dcip2d_survey. No geometry: the export does not carry the ground position of the chainage axis.'");

        // ── 3d. silver.geophysics_dcip_models ────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.geophysics_dcip_models (
                model_pk       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id   uuid NOT NULL,
                project_id     uuid NOT NULL,
                survey_id      uuid NOT NULL,
                source_file    text NOT NULL,
                family         varchar(8) NOT NULL,
                stage          text NOT NULL,
                iteration      integer,
                is_final       boolean NOT NULL DEFAULT false,
                unit           text NOT NULL,
                nx             integer NOT NULL,
                nz             integer NOT NULL,
                cell_values    double precision[] NOT NULL,
                air_mask       boolean[] NOT NULL,
                earth_min      double precision,
                earth_max      double precision,
                earth_median   double precision,
                created_at     timestamptz NOT NULL DEFAULT now(),

                CONSTRAINT chk_geophysics_dcip_models_family
                    CHECK (family IN ('dcinv2d', 'ipinv2d')),
                CONSTRAINT chk_geophysics_dcip_models_shape
                    CHECK (nx > 0 AND nz > 0
                           AND cardinality(cell_values) = nx * nz
                           AND cardinality(air_mask) = nx * nz),
                CONSTRAINT uq_geophysics_dcip_models_stage
                    UNIQUE (survey_id, family, stage),

                CONSTRAINT fk_geophysics_dcip_models_workspace
                    FOREIGN KEY (workspace_id) REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE,
                CONSTRAINT fk_geophysics_dcip_models_project
                    FOREIGN KEY (project_id) REFERENCES silver.projects (project_id)
                    ON DELETE CASCADE,
                CONSTRAINT fk_geophysics_dcip_models_survey
                    FOREIGN KEY (survey_id) REFERENCES silver.geophysics_surveys (survey_id)
                    ON DELETE CASCADE
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geophysics_dcip_models_workspace ON silver.geophysics_dcip_models (workspace_id, project_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geophysics_dcip_models_survey ON silver.geophysics_dcip_models (survey_id)');
        DB::statement("COMMENT ON TABLE silver.geophysics_dcip_models IS
            'ING-19 (2026-09-29): one row per DCIP2D inversion model. cell_values/air_mask are nz rows of nx cells, ROW-MAJOR, SURFACE ROW FIRST. Units: S/m (dcinv2d), mV/V (ipinv2d). Mesh coordinates only — the mesh file that places cells is rarely delivered.'");

        foreach (self::NEW_TABLES as $table) {
            DB::statement("GRANT SELECT, INSERT, UPDATE, DELETE ON {$table} TO georag_app");
            DB::statement("ALTER TABLE {$table} ENABLE ROW LEVEL SECURITY");
            DB::statement("ALTER TABLE {$table} FORCE ROW LEVEL SECURITY");
            DB::statement("DROP POLICY IF EXISTS tenant_isolation ON {$table}");
            DB::statement(
                "CREATE POLICY tenant_isolation ON {$table}"
                ." USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)"
                ." WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)",
            );
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (array_reverse(self::NEW_TABLES) as $table) {
            DB::statement("DROP TABLE IF EXISTS {$table} CASCADE");
        }

        DB::statement('ALTER TABLE silver.geochronology_samples DROP CONSTRAINT IF EXISTS uq_geochron_project_sample');
        DB::statement('ALTER TABLE silver.geochronology_samples DROP CONSTRAINT IF EXISTS chk_geochron_source_epsg');
        DB::statement('DROP INDEX IF EXISTS silver.idx_geochron_project_source');
        DB::statement(<<<'SQL'
            ALTER TABLE silver.geochronology_samples
                DROP COLUMN IF EXISTS source_file,
                DROP COLUMN IF EXISTS source_file_sha256,
                DROP COLUMN IF EXISTS source_object_key,
                DROP COLUMN IF EXISTS source_row,
                DROP COLUMN IF EXISTS parser_name,
                DROP COLUMN IF EXISTS parser_version,
                DROP COLUMN IF EXISTS x_native,
                DROP COLUMN IF EXISTS y_native,
                DROP COLUMN IF EXISTS source_epsg
        SQL);

        DB::statement('ALTER TABLE silver.geophysics_surveys DROP CONSTRAINT IF EXISTS uq_geophysics_surveys_project_name');
        DB::statement('ALTER TABLE silver.geophysics_surveys DROP CONSTRAINT IF EXISTS chk_geophysics_surveys_georef_method');
        DB::statement('ALTER TABLE silver.geophysics_surveys DROP CONSTRAINT IF EXISTS chk_geophysics_surveys_crs_confidence');
        DB::statement(<<<'SQL'
            ALTER TABLE silver.geophysics_surveys
                DROP COLUMN IF EXISTS source_file,
                DROP COLUMN IF EXISTS source_file_sha256,
                DROP COLUMN IF EXISTS source_object_key,
                DROP COLUMN IF EXISTS parser_name,
                DROP COLUMN IF EXISTS parser_version,
                DROP COLUMN IF EXISTS georef_method,
                DROP COLUMN IF EXISTS crs_confidence
        SQL);
        // The old keys are NOT restored: rows written under the per-project
        // keys may legitimately violate them, and failing the rollback on
        // that would be worse than leaving the table unkeyed.
    }
};
