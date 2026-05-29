<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Module 8 Chunk 8.2c — hotfix: correct PL/pgSQL variable-name shadowing in the
 * 3 original silver MVT functions created by 130000_create_silver_mvt_functions.php.
 *
 * Background
 * ──────────
 * Migration 130000 created silver.pg_collars_by_project, silver.pg_drill_traces_by_project,
 * and silver.pg_seismic_by_project each with a DECLARE variable named `project_id`.
 * Inside those functions PostgreSQL resolves the bare identifier `project_id` in WHERE
 * clauses as a table.column reference using the function name as the "table", producing:
 *
 *   ERROR: missing FROM-clause entry for table "pg_collars_by_project"
 *   LINE: WHERE p.project_id = pg_collars_by_project.project_id
 *
 * The same bug in the 4 functions added by migration 140000 was already fixed by
 * migration 140001_fix_silver_mvt_function_variable_names.php using the `v_pid` pattern.
 * This migration applies the identical fix to the remaining 3 functions.
 *
 * Changes per function
 * ────────────────────
 *   DECLARE project_id uuid       → DECLARE v_pid uuid
 *   project_id := (query_params->>'project_id')::uuid   → v_pid := ...
 *   WHERE p.project_id = <fn>.project_id                → WHERE p.project_id = v_pid
 *   WHERE c.project_id = <fn>.project_id                → WHERE c.project_id = v_pid
 *   WHERE dt.project_id = <fn>.project_id               → WHERE dt.project_id = v_pid
 *   WHERE s.project_id = <fn>.project_id                → WHERE s.project_id = v_pid
 *   || project_id::text in md5()                         → || v_pid::text
 *
 * The JSON key 'project_id' passed via query_params is NOT changed — that is the
 * caller-facing API key and was never part of the bug.
 *
 * All other body logic (ETag derivation, ST_TileEnvelope bbox, feature_id synthesis,
 * property columns, zoom-aware simplification, function signatures) is preserved verbatim
 * from migration 130000.
 */
return new class extends Migration
{
    public function up(): void
    {
        // ════════════════════════════════════════════════════════════════════════
        // SILVER FUNCTION 1 — pg_collars_by_project (variable-shadow fix)
        // Source: silver.collars (geometry: Point, EPSG:32613 → 3857)
        // PK:     collar_id (uuid)
        // ════════════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_collars_by_project(
                z integer,
                x integer,
                y integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql STABLE PARALLEL SAFE
            AS $$
            DECLARE
                v_pid     uuid;
                v         bigint;
                tile_bbox geometry;
            BEGIN
                -- Step 1: extract and validate project_id from caller JSON
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                -- Step 2: get data_version; return empty if project row missing
                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid;

                IF NOT FOUND THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                -- Step 3: compute tile bbox in EPSG:3857
                tile_bbox := ST_TileEnvelope(z, x, y);

                -- Steps 4-7: build tile, compute etag
                RETURN QUERY
                WITH tile AS (
                    SELECT
                        -- feature_id: stable uint64 from UUID PK
                        (hashtext(c.collar_id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        c.project_id                AS project_id,
                        c.hole_id                   AS hole_id,
                        c.azimuth                   AS collar_azimuth,
                        c.dip                       AS collar_dip,
                        c.total_depth               AS total_depth_m,
                        -- Points: no simplification. Transform 32613→3857 then clip.
                        ST_AsMVTGeom(
                            ST_Transform(c.geom, 3857),
                            tile_bbox,
                            4096,
                            64,
                            true
                        ) AS geom
                    FROM silver.collars c
                    WHERE c.project_id = v_pid
                      AND ST_Intersects(
                            ST_Transform(c.geom, 3857),
                            tile_bbox
                        )
                )
                SELECT
                    ST_AsMVT(tile, 'collars', 4096, 'geom') AS mvt,
                    md5(
                        v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text
                    ) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_collars_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature: RETURNS TABLE(mvt bytea, etag_hash text). Source: silver.collars (EPSG:32613). Transforms to 3857 for tile envelope intersection. ETag = md5(data_version|z|x|y|project_id). Variable-shadow hotfix applied by 140002 (v_pid). Module 8 Chunk 8.1/8.2/8.2c.'");

        DB::statement('GRANT EXECUTE ON FUNCTION silver.pg_collars_by_project(integer, integer, integer, json) TO martin_readonly;');

        // ════════════════════════════════════════════════════════════════════════
        // SILVER FUNCTION 2 — pg_drill_traces_by_project (variable-shadow fix)
        // Source: silver.drill_traces JOIN silver.collars (EPSG:4326 → 3857)
        // PK:     trace_id (uuid)
        // ════════════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_drill_traces_by_project(
                z integer,
                x integer,
                y integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql STABLE PARALLEL SAFE
            AS $$
            DECLARE
                v_pid     uuid;
                v         bigint;
                tile_bbox geometry;
                simp_tol  double precision;
            BEGIN
                -- Step 1
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                -- Step 2
                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid;

                IF NOT FOUND THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                -- Step 3
                tile_bbox := ST_TileEnvelope(z, x, y);

                -- Zoom-aware simplification tolerance (metres at EPSG:3857 equator scale)
                simp_tol := CASE
                    WHEN z < 8  THEN 100.0
                    WHEN z < 12 THEN 25.0
                    ELSE             5.0
                END;

                -- Steps 4-7
                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(dt.trace_id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        dt.project_id                   AS project_id,
                        c.hole_id                       AS hole_id,
                        c.total_depth                   AS total_depth_m,
                        ST_AsMVTGeom(
                            ST_SimplifyPreserveTopology(
                                ST_Transform(dt.geom, 3857),
                                simp_tol
                            ),
                            tile_bbox,
                            4096,
                            64,
                            true
                        ) AS geom
                    FROM silver.drill_traces dt
                    JOIN silver.collars c ON c.collar_id = dt.collar_id
                    WHERE dt.project_id = v_pid
                      AND ST_Intersects(
                            ST_Transform(dt.geom, 3857),
                            tile_bbox
                        )
                )
                SELECT
                    ST_AsMVT(tile, 'drill_traces', 4096, 'geom') AS mvt,
                    md5(
                        v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text
                    ) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_drill_traces_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.drill_traces JOIN silver.collars (EPSG:4326→3857). Zoom-aware simplification on linestring geometry. Variable-shadow hotfix applied by 140002 (v_pid). Module 8 Chunk 8.1/8.2/8.2c.'");

        DB::statement('GRANT EXECUTE ON FUNCTION silver.pg_drill_traces_by_project(integer, integer, integer, json) TO martin_readonly;');

        // ════════════════════════════════════════════════════════════════════════
        // SILVER FUNCTION 3 — pg_seismic_by_project (variable-shadow fix)
        // Source: silver.seismic_surveys.bbox (geometry: Polygon, EPSG:4326 → 3857)
        // PK:     survey_id (uuid)
        // ════════════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_seismic_by_project(
                z integer,
                x integer,
                y integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql STABLE PARALLEL SAFE
            AS $$
            DECLARE
                v_pid     uuid;
                v         bigint;
                tile_bbox geometry;
                simp_tol  double precision;
            BEGIN
                -- Step 1
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                -- Step 2
                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid;

                IF NOT FOUND THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                -- Step 3
                tile_bbox := ST_TileEnvelope(z, x, y);

                -- Zoom-aware simplification tolerance
                simp_tol := CASE
                    WHEN z < 8  THEN 100.0
                    WHEN z < 12 THEN 25.0
                    ELSE             5.0
                END;

                -- Steps 4-7
                -- survey_year: derived from created_at (no dedicated column in schema).
                -- line_count: aliased from num_traces (num seismic traces — nearest
                --   available proxy for line count; SME to confirm or add column).
                -- bbox geometry column used (not geom); named geom in tile output.
                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(s.survey_id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        s.project_id                                AS project_id,
                        s.survey_name                               AS survey_name,
                        EXTRACT(YEAR FROM s.created_at)::int        AS survey_year,
                        s.survey_type                               AS survey_type,
                        s.num_traces                                AS line_count,
                        ST_AsMVTGeom(
                            ST_SimplifyPreserveTopology(
                                ST_Transform(s.bbox, 3857),
                                simp_tol
                            ),
                            tile_bbox,
                            4096,
                            64,
                            true
                        ) AS geom
                    FROM silver.seismic_surveys s
                    WHERE s.project_id = v_pid
                      AND s.bbox IS NOT NULL
                      AND ST_Intersects(
                            ST_Transform(s.bbox, 3857),
                            tile_bbox
                        )
                )
                SELECT
                    ST_AsMVT(tile, 'seismic', 4096, 'geom') AS mvt,
                    md5(
                        v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text
                    ) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_seismic_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.seismic_surveys.bbox (EPSG:4326→3857). survey_year derived from created_at; line_count aliased from num_traces — SME to confirm. Variable-shadow hotfix applied by 140002 (v_pid). Pre-approved V1 item (2026-04-22). Module 8 Chunk 8.1/8.2/8.2c.'");

        DB::statement('GRANT EXECUTE ON FUNCTION silver.pg_seismic_by_project(integer, integer, integer, json) TO martin_readonly;');
    }

    public function down(): void
    {
        // No-op: rolling back to the broken bodies is not useful.
        // Rolling back 130000 (which drops the functions) is the correct
        // full-rollback path if needed.
    }
};
