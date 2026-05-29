<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Two related additions for the §B/S/G build-out:
 *
 *   1. silver.pg_cross_section_lines_by_project — Martin MVT function-source
 *      that serves the LineString section lines from gold.cross_section_panels
 *      so the MapLibre layer can draw them on the project map.
 *
 *   2. UNIQUE (workspace_id, survey_name) on silver.geophysics_surveys — so
 *      callers that omit an explicit survey_id (the JSON-payload writer's
 *      common path) still get deterministic UPSERT semantics. The original
 *      table had only survey_id PK; same payload posted twice would create
 *      two rows.
 *
 * Idempotent: CREATE OR REPLACE FUNCTION + IF NOT EXISTS on the constraint.
 * SQLite — gated on Postgres (PostGIS-only).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // ════════════════════════════════════════════════════════════════════
        // 1. silver.pg_cross_section_lines_by_project — Martin tile function
        // Source: gold.cross_section_panels.section_line_geom (LineString, 4326)
        // Properties: project_id, panel_id, section_name, azimuth_deg, length_m,
        //             buffer_m, hole_count (jsonb_array_length of collars_projected)
        // Simplification: ST_SimplifyPreserveTopology with zoom-aware tolerance.
        // ════════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_cross_section_lines_by_project(
                z integer,
                x integer,
                y integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            AS $$
            DECLARE
                project_id uuid;
                v          bigint;
                tile_bbox  geometry;
                tolerance  double precision;
            BEGIN
                project_id := (query_params->>'project_id')::uuid;
                IF project_id IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                SELECT p.data_version INTO v
                  FROM silver.projects p
                 WHERE p.project_id = pg_cross_section_lines_by_project.project_id;

                IF NOT FOUND THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                tile_bbox := ST_TileEnvelope(z, x, y);
                tolerance := GREATEST(0.5, 156543.034 / (2 ^ z) * 0.5);

                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(p.panel_id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        p.project_id   AS project_id,
                        p.section_name AS section_name,
                        p.azimuth_deg  AS azimuth_deg,
                        p.length_m     AS length_m,
                        p.buffer_m     AS buffer_m,
                        jsonb_array_length(p.collars_projected) AS hole_count,
                        ST_AsMVTGeom(
                            ST_SimplifyPreserveTopology(
                                ST_Transform(p.section_line_geom, 3857),
                                tolerance
                            ),
                            tile_bbox,
                            4096,
                            64,
                            true
                        ) AS geom
                    FROM gold.cross_section_panels p
                    WHERE p.project_id = pg_cross_section_lines_by_project.project_id
                      AND ST_Intersects(
                            ST_Transform(p.section_line_geom, 3857),
                            tile_bbox
                        )
                )
                SELECT
                    ST_AsMVT(tile, 'cross_section_lines', 4096, 'geom') AS mvt,
                    md5(
                        v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || project_id::text
                    ) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_cross_section_lines_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature: RETURNS TABLE(mvt bytea, etag_hash text). Source: gold.cross_section_panels.section_line_geom (EPSG:4326). Transforms to 3857 for tile envelope intersection. Zoom-aware ST_SimplifyPreserveTopology. ETag = md5(data_version|z|x|y|project_id). §B/S/G build-out 2026-05-22.'");

        DB::statement('GRANT EXECUTE ON FUNCTION silver.pg_cross_section_lines_by_project(integer, integer, integer, json) TO martin_readonly;');

        // The function reads gold.cross_section_panels — martin_readonly
        // needs SELECT to actually return rows. Without this the function
        // executes but returns empty tiles. Same pattern as Chunk 8.3
        // schema-level grants on silver.* tables.
        DB::statement('GRANT USAGE ON SCHEMA gold TO martin_readonly;');
        DB::statement('GRANT SELECT ON gold.cross_section_panels TO martin_readonly;');
        DB::statement('GRANT SELECT ON gold.structure_measurements_visual TO martin_readonly;');
        DB::statement('GRANT SELECT ON gold.drillhole_intervals_visual TO martin_readonly;');

        // ════════════════════════════════════════════════════════════════════
        // 2. UNIQUE (workspace_id, survey_name) on silver.geophysics_surveys
        // Lets the payload writer UPSERT by survey_name when survey_id is omitted.
        // ════════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.table_constraints
                     WHERE table_schema = 'silver'
                       AND table_name   = 'geophysics_surveys'
                       AND constraint_name = 'uq_geophysics_surveys_workspace_name'
                ) THEN
                    ALTER TABLE silver.geophysics_surveys
                        ADD CONSTRAINT uq_geophysics_surveys_workspace_name
                        UNIQUE (workspace_id, survey_name);
                END IF;
            END $$;
        SQL);

        DB::statement("COMMENT ON CONSTRAINT uq_geophysics_surveys_workspace_name ON silver.geophysics_surveys IS
            'Deterministic UPSERT key for silver_geophysics writer when payload omits survey_id. Same name re-posted in the same workspace updates instead of duplicating.'");
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }
        DB::statement('ALTER TABLE silver.geophysics_surveys DROP CONSTRAINT IF EXISTS uq_geophysics_surveys_workspace_name');
        DB::statement('DROP FUNCTION IF EXISTS silver.pg_cross_section_lines_by_project(integer, integer, integer, json)');
    }
};
