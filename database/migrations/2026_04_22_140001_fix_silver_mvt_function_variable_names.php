<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Module 8 Chunk 8.2b — hotfix: correct PL/pgSQL variable naming in the 4 new silver MVT functions.
 *
 * The 140000 migration created the 4 functions using `project_id` as the DECLARE variable name.
 * Inside the WHERE clause of the data_version lookup SELECT, PostgreSQL disambiguates
 * `pg_boundaries_by_project.project_id` as a table.column reference (failing with
 * "missing FROM-clause entry for table") rather than recognising it as a function-name
 * qualifier for the PL/pgSQL local variable.
 *
 * Fix: rename the local variable from `project_id` to `v_pid` in all 4 functions.
 * The existing 8.1 functions (pg_collars_by_project, pg_drill_traces_by_project,
 * pg_seismic_by_project) have the same pattern and are NOT touched here — that is
 * a pre-existing issue tracked for 8.3 schema-level grant work.
 *
 * Also adds GRANT SELECT ON silver.projects, silver.workspaces TO martin_readonly
 * so the functions can execute the data_version lookup when Martin calls them.
 * (Chunk 8.3 owns the full schema-level grant pass; these two grants are the minimum
 * needed for the functions defined in this chunk to work.)
 */
return new class extends Migration
{
    public function up(): void
    {
        // Minimum grants needed for the 4 new functions to run via Martin (georag user)
        DB::statement('GRANT SELECT ON silver.projects TO martin_readonly;');
        DB::statement('GRANT SELECT ON silver.workspaces TO martin_readonly;');

        // ── pg_boundaries_by_project — fixed ─────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_boundaries_by_project(
                z            integer,
                x            integer,
                y            integer,
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
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid;

                IF NOT FOUND THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                tile_bbox := ST_TileEnvelope(z, x, y);
                simp_tol := CASE
                    WHEN z < 8  THEN 100.0
                    WHEN z < 12 THEN 25.0
                    ELSE             5.0
                END;

                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(b.id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        b.project_id    AS project_id,
                        b.boundary_name AS boundary_name,
                        b.boundary_type AS boundary_type,
                        b.effective_from AS effective_from,
                        b.effective_to  AS effective_to,
                        ST_AsMVTGeom(
                            ST_SimplifyPreserveTopology(
                                ST_Transform(b.geom, 3857), simp_tol
                            ),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM silver.project_boundaries b
                    WHERE b.project_id = v_pid
                      AND ST_Intersects(ST_Transform(b.geom, 3857), tile_bbox)
                )
                SELECT
                    ST_AsMVT(tile, 'boundaries', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        // ── pg_formations_by_project — fixed ─────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_formations_by_project(
                z            integer,
                x            integer,
                y            integer,
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
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid;

                IF NOT FOUND THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                tile_bbox := ST_TileEnvelope(z, x, y);
                simp_tol := CASE
                    WHEN z < 8  THEN 100.0
                    WHEN z < 12 THEN 25.0
                    ELSE             5.0
                END;

                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(f.id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        f.project_id      AS project_id,
                        f.formation_code  AS formation_code,
                        f.formation_name  AS formation_name,
                        f.age_period      AS age_period,
                        f.age_ma_lower    AS age_ma_lower,
                        f.age_ma_upper    AS age_ma_upper,
                        f.lithology_primary AS lithology_primary,
                        ST_AsMVTGeom(
                            ST_SimplifyPreserveTopology(
                                ST_Transform(f.geom, 3857), simp_tol
                            ),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM silver.geological_formations f
                    WHERE f.project_id = v_pid
                      AND ST_Intersects(ST_Transform(f.geom, 3857), tile_bbox)
                )
                SELECT
                    ST_AsMVT(tile, 'formations', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        // ── pg_historic_workings_by_project — fixed ───────────────────────────
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_historic_workings_by_project(
                z            integer,
                x            integer,
                y            integer,
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
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid;

                IF NOT FOUND THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                tile_bbox := ST_TileEnvelope(z, x, y);

                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(hw.id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        hw.project_id            AS project_id,
                        hw.working_name          AS working_name,
                        hw.working_type          AS working_type,
                        hw.operational_period    AS operational_period,
                        hw.operational_from_year AS operational_from_year,
                        hw.operational_to_year   AS operational_to_year,
                        to_json(hw.commodity_codes)::text AS commodity_codes,
                        hw.status                AS status,
                        ST_AsMVTGeom(
                            ST_Transform(hw.geom, 3857),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM silver.historic_workings hw
                    WHERE hw.project_id = v_pid
                      AND ST_Intersects(ST_Transform(hw.geom, 3857), tile_bbox)
                )
                SELECT
                    ST_AsMVT(tile, 'historic_workings', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        // ── pg_geochem_by_project — fixed ────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_geochem_by_project(
                z            integer,
                x            integer,
                y            integer,
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
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid;

                IF NOT FOUND THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                tile_bbox := ST_TileEnvelope(z, x, y);

                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(gc.geochem_id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        gc.project_id                         AS project_id,
                        gc.sample_id                          AS sample_id,
                        gc.sample_type                        AS sample_type,
                        to_json(gc.assay_element_codes)::text AS assay_element_codes,
                        gc.collar_id                          AS collar_id,
                        ST_AsMVTGeom(
                            ST_Transform(gc.geom, 3857),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM silver.geochemistry gc
                    WHERE gc.project_id = v_pid
                      AND gc.geom IS NOT NULL
                      AND ST_Intersects(ST_Transform(gc.geom, 3857), tile_bbox)
                )
                SELECT
                    ST_AsMVT(tile, 'geochem', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        // Re-grant EXECUTE to martin_readonly (CREATE OR REPLACE preserves grants in
        // PostgreSQL, but being explicit is safer after a function body replacement).
        DB::statement('GRANT EXECUTE ON FUNCTION silver.pg_boundaries_by_project(integer, integer, integer, json) TO martin_readonly;');
        DB::statement('GRANT EXECUTE ON FUNCTION silver.pg_formations_by_project(integer, integer, integer, json) TO martin_readonly;');
        DB::statement('GRANT EXECUTE ON FUNCTION silver.pg_historic_workings_by_project(integer, integer, integer, json) TO martin_readonly;');
        DB::statement('GRANT EXECUTE ON FUNCTION silver.pg_geochem_by_project(integer, integer, integer, json) TO martin_readonly;');
    }

    public function down(): void
    {
        // No-op on rollback: the 140000 down() drops the functions entirely.
        // Rolling back 140001 alone (without rolling back 140000) would leave the
        // broken v1 function bodies; acceptable since these are re-replaced by the
        // next forward migration run.
    }
};
