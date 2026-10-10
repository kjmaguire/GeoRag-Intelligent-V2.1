<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Seven MVT tile functions prefilter on the 4326 column (GIS audit 2026-10,
 * finding 15).
 *
 * pg_drill_traces_by_project, pg_seismic_by_project, pg_boundaries_by_project,
 * pg_formations_by_project, pg_historic_workings_by_project,
 * pg_geochem_by_project and pg_cross_section_lines_by_project (all from
 * 2026_09_16_120000) pick a tile's features with
 *
 *     ST_Intersects(ST_Transform(col, 3857), tile_bbox)
 *
 * The column is EPSG:4326 and has a GiST index, but a predicate on a
 * TRANSFORMED column cannot use it: every request for every tile reprojected
 * every row of the project (and, since the project_id / workspace_id filters
 * are the only index-assisted part, a project with tens of thousands of drill
 * traces did that tens of thousands of times per tile, per pan and zoom).
 *
 * Each now also asks `col && tile_4326`, where tile_4326 is the tile envelope
 * transformed to 4326 once. A Web Mercator tile envelope is an axis-aligned
 * lon/lat rectangle (x is linear in longitude, y monotonic in latitude), so
 * transforming the ENVELOPE is exact, and a feature whose 3857 geometry meets
 * the tile necessarily has a 4326 bounding box that meets tile_4326. The
 * bounding-box test is therefore a pure prefilter: it can only discard
 * features the exact ST_Intersects below it, which is kept unchanged, would
 * have discarded too. Same tiles, byte for byte, reached through the index:
 * the 893 tiles z0-z14 over a 20 000-trace test project were identical (MVT
 * bytes and etag) for all seven functions before and after.
 *
 * Measured on that project (PostGIS 3.4, one tile function call per tile):
 * z11 24.4 s -> 0.39 s for 169 tiles; z12 137 s -> 0.23 s for 600 tiles
 * (EXPLAIN of one z12 tile: a nested loop over all 20 000 collars, 94.6 ms,
 * becomes a bitmap scan of idx_drill_traces_geom, 0.7 ms); z13-14 10.7 s ->
 * 0.24 s for 47 tiles. Tiles that hold most of the traces (z <= 10) are
 * dominated by simplifying them and went from 49 s to 25.5 s for 77 tiles.
 *
 * ONLY THAT CHANGES. Signatures, workspace scoping and its raises, the
 * set_config, volatility, the published properties, ORDER BY and the etag are
 * copied from 2026_09_16_120000; every body differs from it by a `tile_4326`
 * variable, its assignment and one predicate line (the generator that wrote
 * this file asserted each of the three fired exactly once per function).
 *
 * Not touched, because they already do this: pg_collars_by_project
 * (2026_09_29_200100: geom_4326 && tile_4326) and pg_spatial_features_by_project
 * (2026_08_23_120000: sf.geom && bbox_4326).
 *
 * CREATE OR REPLACE keeps each function's grants and comment.
 *
 * down() restores the 2026_09_16_120000 bodies, verbatim. (That migration's own
 * down() is a deliberate no-op because reverting ITS change would reopen a
 * cross-tenant leak; this change touches no scoping, so it can be reverted.)
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // silver.pg_drill_traces_by_project
        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_drill_traces_by_project(
                z integer,
                x integer,
                y integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql
            AS $$
            DECLARE
                v_pid     uuid;
                v_wsid    uuid;
                v         bigint;
                tile_bbox geometry;
                tile_4326 geometry;
                simp_tol  double precision;
            BEGIN
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                BEGIN
                    v_wsid := NULLIF(query_params->>'workspace_id', '')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'silver.pg_drill_traces_by_project: workspace_id in query_params is not a valid UUID';
                END;
                IF v_wsid IS NULL THEN
                    RAISE EXCEPTION 'silver.pg_drill_traces_by_project: workspace_id is required in query_params (tenant-scoped tile source)';
                END IF;

                PERFORM set_config('app.workspace_id', v_wsid::text, true);

                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid
                  AND p.workspace_id = v_wsid;

                IF NOT FOUND THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                tile_bbox := ST_TileEnvelope(z, x, y);
                -- A Web Mercator tile is an axis-aligned lon/lat rectangle, so
                -- transforming the ENVELOPE to 4326 is exact; it lets the column's
                -- GiST index answer the tile (GIS audit 2026-10).
                tile_4326 := ST_Transform(tile_bbox, 4326);

                simp_tol := CASE
                    WHEN z < 8  THEN 100.0
                    WHEN z < 12 THEN 25.0
                    ELSE             5.0
                END;

                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(dt.trace_id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        dt.project_id                   AS project_id,
                        c.hole_id                       AS hole_id,
                        c.total_depth                   AS total_depth_m,
                        ST_AsMVTGeom(
                            ST_SimplifyPreserveTopology(
                                ST_Transform(dt.geom, 3857), simp_tol
                            ),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM silver.drill_traces dt
                    JOIN silver.collars c ON c.collar_id = dt.collar_id
                    WHERE dt.project_id = v_pid
                      AND dt.workspace_id = v_wsid
                      AND dt.geom && tile_4326
                      AND ST_Intersects(ST_Transform(dt.geom, 3857), tile_bbox)
                    ORDER BY dt.trace_id
                )
                SELECT
                    ST_AsMVT(tile, 'drill_traces', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text || '|' || v_wsid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        // silver.pg_seismic_by_project
        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_seismic_by_project(
                z integer,
                x integer,
                y integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql
            AS $$
            DECLARE
                v_pid     uuid;
                v_wsid    uuid;
                v         bigint;
                tile_bbox geometry;
                tile_4326 geometry;
                simp_tol  double precision;
            BEGIN
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                BEGIN
                    v_wsid := NULLIF(query_params->>'workspace_id', '')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'silver.pg_seismic_by_project: workspace_id in query_params is not a valid UUID';
                END;
                IF v_wsid IS NULL THEN
                    RAISE EXCEPTION 'silver.pg_seismic_by_project: workspace_id is required in query_params (tenant-scoped tile source)';
                END IF;

                PERFORM set_config('app.workspace_id', v_wsid::text, true);

                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid
                  AND p.workspace_id = v_wsid;

                IF NOT FOUND THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                tile_bbox := ST_TileEnvelope(z, x, y);
                -- A Web Mercator tile is an axis-aligned lon/lat rectangle, so
                -- transforming the ENVELOPE to 4326 is exact; it lets the column's
                -- GiST index answer the tile (GIS audit 2026-10).
                tile_4326 := ST_Transform(tile_bbox, 4326);

                simp_tol := CASE
                    WHEN z < 8  THEN 100.0
                    WHEN z < 12 THEN 25.0
                    ELSE             5.0
                END;

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
                                ST_Transform(s.bbox, 3857), simp_tol
                            ),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM silver.seismic_surveys s
                    WHERE s.project_id = v_pid
                      AND s.workspace_id = v_wsid
                      AND s.bbox IS NOT NULL
                      AND s.bbox && tile_4326
                      AND ST_Intersects(ST_Transform(s.bbox, 3857), tile_bbox)
                    ORDER BY s.survey_id
                )
                SELECT
                    ST_AsMVT(tile, 'seismic', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text || '|' || v_wsid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        // silver.pg_boundaries_by_project
        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_boundaries_by_project(
                z            integer,
                x            integer,
                y            integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql
            AS $$
            DECLARE
                v_pid     uuid;
                v_wsid    uuid;
                v         bigint;
                tile_bbox geometry;
                tile_4326 geometry;
                simp_tol  double precision;
            BEGIN
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                BEGIN
                    v_wsid := NULLIF(query_params->>'workspace_id', '')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'silver.pg_boundaries_by_project: workspace_id in query_params is not a valid UUID';
                END;
                IF v_wsid IS NULL THEN
                    RAISE EXCEPTION 'silver.pg_boundaries_by_project: workspace_id is required in query_params (tenant-scoped tile source)';
                END IF;

                PERFORM set_config('app.workspace_id', v_wsid::text, true);

                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid
                  AND p.workspace_id = v_wsid;

                IF NOT FOUND THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                tile_bbox := ST_TileEnvelope(z, x, y);
                -- A Web Mercator tile is an axis-aligned lon/lat rectangle, so
                -- transforming the ENVELOPE to 4326 is exact; it lets the column's
                -- GiST index answer the tile (GIS audit 2026-10).
                tile_4326 := ST_Transform(tile_bbox, 4326);
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
                      AND b.workspace_id = v_wsid
                      AND b.geom && tile_4326
                      AND ST_Intersects(ST_Transform(b.geom, 3857), tile_bbox)
                    ORDER BY b.id
                )
                SELECT
                    ST_AsMVT(tile, 'boundaries', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text || '|' || v_wsid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        // silver.pg_formations_by_project
        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_formations_by_project(
                z            integer,
                x            integer,
                y            integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql
            AS $$
            DECLARE
                v_pid     uuid;
                v_wsid    uuid;
                v         bigint;
                tile_bbox geometry;
                tile_4326 geometry;
                simp_tol  double precision;
            BEGIN
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                BEGIN
                    v_wsid := NULLIF(query_params->>'workspace_id', '')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'silver.pg_formations_by_project: workspace_id in query_params is not a valid UUID';
                END;
                IF v_wsid IS NULL THEN
                    RAISE EXCEPTION 'silver.pg_formations_by_project: workspace_id is required in query_params (tenant-scoped tile source)';
                END IF;

                PERFORM set_config('app.workspace_id', v_wsid::text, true);

                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid
                  AND p.workspace_id = v_wsid;

                IF NOT FOUND THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                tile_bbox := ST_TileEnvelope(z, x, y);
                -- A Web Mercator tile is an axis-aligned lon/lat rectangle, so
                -- transforming the ENVELOPE to 4326 is exact; it lets the column's
                -- GiST index answer the tile (GIS audit 2026-10).
                tile_4326 := ST_Transform(tile_bbox, 4326);
                simp_tol := CASE
                    WHEN z < 8  THEN 100.0
                    WHEN z < 12 THEN 25.0
                    ELSE             5.0
                END;

                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(f.id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        f.project_id        AS project_id,
                        f.formation_code    AS formation_code,
                        f.formation_name    AS formation_name,
                        f.age_period        AS age_period,
                        f.age_ma_lower      AS age_ma_lower,
                        f.age_ma_upper      AS age_ma_upper,
                        f.lithology_primary AS lithology_primary,
                        ST_AsMVTGeom(
                            ST_SimplifyPreserveTopology(
                                ST_Transform(f.geom, 3857), simp_tol
                            ),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM silver.geological_formations f
                    WHERE f.project_id = v_pid
                      AND f.workspace_id = v_wsid
                      AND f.geom && tile_4326
                      AND ST_Intersects(ST_Transform(f.geom, 3857), tile_bbox)
                    ORDER BY f.id
                )
                SELECT
                    ST_AsMVT(tile, 'formations', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text || '|' || v_wsid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        // silver.pg_historic_workings_by_project
        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_historic_workings_by_project(
                z            integer,
                x            integer,
                y            integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql
            AS $$
            DECLARE
                v_pid     uuid;
                v_wsid    uuid;
                v         bigint;
                tile_bbox geometry;
                tile_4326 geometry;
            BEGIN
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                BEGIN
                    v_wsid := NULLIF(query_params->>'workspace_id', '')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'silver.pg_historic_workings_by_project: workspace_id in query_params is not a valid UUID';
                END;
                IF v_wsid IS NULL THEN
                    RAISE EXCEPTION 'silver.pg_historic_workings_by_project: workspace_id is required in query_params (tenant-scoped tile source)';
                END IF;

                PERFORM set_config('app.workspace_id', v_wsid::text, true);

                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid
                  AND p.workspace_id = v_wsid;

                IF NOT FOUND THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                tile_bbox := ST_TileEnvelope(z, x, y);
                -- A Web Mercator tile is an axis-aligned lon/lat rectangle, so
                -- transforming the ENVELOPE to 4326 is exact; it lets the column's
                -- GiST index answer the tile (GIS audit 2026-10).
                tile_4326 := ST_Transform(tile_bbox, 4326);

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
                      AND hw.workspace_id = v_wsid
                      AND hw.geom && tile_4326
                      AND ST_Intersects(ST_Transform(hw.geom, 3857), tile_bbox)
                    ORDER BY hw.id
                )
                SELECT
                    ST_AsMVT(tile, 'historic_workings', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text || '|' || v_wsid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        // silver.pg_geochem_by_project
        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_geochem_by_project(
                z            integer,
                x            integer,
                y            integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql
            AS $$
            DECLARE
                v_pid     uuid;
                v_wsid    uuid;
                v         bigint;
                tile_bbox geometry;
                tile_4326 geometry;
            BEGIN
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                BEGIN
                    v_wsid := NULLIF(query_params->>'workspace_id', '')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'silver.pg_geochem_by_project: workspace_id in query_params is not a valid UUID';
                END;
                IF v_wsid IS NULL THEN
                    RAISE EXCEPTION 'silver.pg_geochem_by_project: workspace_id is required in query_params (tenant-scoped tile source)';
                END IF;

                PERFORM set_config('app.workspace_id', v_wsid::text, true);

                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid
                  AND p.workspace_id = v_wsid;

                IF NOT FOUND THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                tile_bbox := ST_TileEnvelope(z, x, y);
                -- A Web Mercator tile is an axis-aligned lon/lat rectangle, so
                -- transforming the ENVELOPE to 4326 is exact; it lets the column's
                -- GiST index answer the tile (GIS audit 2026-10).
                tile_4326 := ST_Transform(tile_bbox, 4326);

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
                      AND gc.workspace_id = v_wsid
                      AND gc.geom IS NOT NULL
                      AND gc.geom && tile_4326
                      AND ST_Intersects(ST_Transform(gc.geom, 3857), tile_bbox)
                    ORDER BY gc.geochem_id
                )
                SELECT
                    ST_AsMVT(tile, 'geochem', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text || '|' || v_wsid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        // silver.pg_cross_section_lines_by_project
        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_cross_section_lines_by_project(
                z integer,
                x integer,
                y integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql
            AS $$
            DECLARE
                v_pid     uuid;
                v_wsid    uuid;
                v         bigint;
                tile_bbox geometry;
                tile_4326 geometry;
                tolerance double precision;
            BEGIN
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                BEGIN
                    v_wsid := NULLIF(query_params->>'workspace_id', '')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'silver.pg_cross_section_lines_by_project: workspace_id in query_params is not a valid UUID';
                END;
                IF v_wsid IS NULL THEN
                    RAISE EXCEPTION 'silver.pg_cross_section_lines_by_project: workspace_id is required in query_params (tenant-scoped tile source)';
                END IF;

                PERFORM set_config('app.workspace_id', v_wsid::text, true);

                SELECT p.data_version INTO v
                  FROM silver.projects p
                 WHERE p.project_id = v_pid
                   AND p.workspace_id = v_wsid;

                IF NOT FOUND THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                tile_bbox := ST_TileEnvelope(z, x, y);
                -- A Web Mercator tile is an axis-aligned lon/lat rectangle, so
                -- transforming the ENVELOPE to 4326 is exact; it lets the column's
                -- GiST index answer the tile (GIS audit 2026-10).
                tile_4326 := ST_Transform(tile_bbox, 4326);
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
                    WHERE p.project_id = v_pid
                      AND p.workspace_id = v_wsid
                      AND p.section_line_geom && tile_4326
                      AND ST_Intersects(
                            ST_Transform(p.section_line_geom, 3857),
                            tile_bbox
                        )
                    ORDER BY p.panel_id
                )
                SELECT
                    ST_AsMVT(tile, 'cross_section_lines', 4096, 'geom') AS mvt,
                    md5(
                        v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text || '|' || v_wsid::text
                    ) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // 2026_09_16_120000 up(), verbatim.
        // silver.pg_drill_traces_by_project
        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_drill_traces_by_project(
                z integer,
                x integer,
                y integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql
            AS $$
            DECLARE
                v_pid     uuid;
                v_wsid    uuid;
                v         bigint;
                tile_bbox geometry;
                simp_tol  double precision;
            BEGIN
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                BEGIN
                    v_wsid := NULLIF(query_params->>'workspace_id', '')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'silver.pg_drill_traces_by_project: workspace_id in query_params is not a valid UUID';
                END;
                IF v_wsid IS NULL THEN
                    RAISE EXCEPTION 'silver.pg_drill_traces_by_project: workspace_id is required in query_params (tenant-scoped tile source)';
                END IF;

                PERFORM set_config('app.workspace_id', v_wsid::text, true);

                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid
                  AND p.workspace_id = v_wsid;

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
                        (hashtext(dt.trace_id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        dt.project_id                   AS project_id,
                        c.hole_id                       AS hole_id,
                        c.total_depth                   AS total_depth_m,
                        ST_AsMVTGeom(
                            ST_SimplifyPreserveTopology(
                                ST_Transform(dt.geom, 3857), simp_tol
                            ),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM silver.drill_traces dt
                    JOIN silver.collars c ON c.collar_id = dt.collar_id
                    WHERE dt.project_id = v_pid
                      AND dt.workspace_id = v_wsid
                      AND ST_Intersects(ST_Transform(dt.geom, 3857), tile_bbox)
                    ORDER BY dt.trace_id
                )
                SELECT
                    ST_AsMVT(tile, 'drill_traces', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text || '|' || v_wsid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        // silver.pg_seismic_by_project
        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_seismic_by_project(
                z integer,
                x integer,
                y integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql
            AS $$
            DECLARE
                v_pid     uuid;
                v_wsid    uuid;
                v         bigint;
                tile_bbox geometry;
                simp_tol  double precision;
            BEGIN
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                BEGIN
                    v_wsid := NULLIF(query_params->>'workspace_id', '')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'silver.pg_seismic_by_project: workspace_id in query_params is not a valid UUID';
                END;
                IF v_wsid IS NULL THEN
                    RAISE EXCEPTION 'silver.pg_seismic_by_project: workspace_id is required in query_params (tenant-scoped tile source)';
                END IF;

                PERFORM set_config('app.workspace_id', v_wsid::text, true);

                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid
                  AND p.workspace_id = v_wsid;

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
                        (hashtext(s.survey_id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        s.project_id                                AS project_id,
                        s.survey_name                               AS survey_name,
                        EXTRACT(YEAR FROM s.created_at)::int        AS survey_year,
                        s.survey_type                               AS survey_type,
                        s.num_traces                                AS line_count,
                        ST_AsMVTGeom(
                            ST_SimplifyPreserveTopology(
                                ST_Transform(s.bbox, 3857), simp_tol
                            ),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM silver.seismic_surveys s
                    WHERE s.project_id = v_pid
                      AND s.workspace_id = v_wsid
                      AND s.bbox IS NOT NULL
                      AND ST_Intersects(ST_Transform(s.bbox, 3857), tile_bbox)
                    ORDER BY s.survey_id
                )
                SELECT
                    ST_AsMVT(tile, 'seismic', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text || '|' || v_wsid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        // silver.pg_boundaries_by_project
        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_boundaries_by_project(
                z            integer,
                x            integer,
                y            integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql
            AS $$
            DECLARE
                v_pid     uuid;
                v_wsid    uuid;
                v         bigint;
                tile_bbox geometry;
                simp_tol  double precision;
            BEGIN
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                BEGIN
                    v_wsid := NULLIF(query_params->>'workspace_id', '')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'silver.pg_boundaries_by_project: workspace_id in query_params is not a valid UUID';
                END;
                IF v_wsid IS NULL THEN
                    RAISE EXCEPTION 'silver.pg_boundaries_by_project: workspace_id is required in query_params (tenant-scoped tile source)';
                END IF;

                PERFORM set_config('app.workspace_id', v_wsid::text, true);

                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid
                  AND p.workspace_id = v_wsid;

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
                      AND b.workspace_id = v_wsid
                      AND ST_Intersects(ST_Transform(b.geom, 3857), tile_bbox)
                    ORDER BY b.id
                )
                SELECT
                    ST_AsMVT(tile, 'boundaries', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text || '|' || v_wsid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        // silver.pg_formations_by_project
        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_formations_by_project(
                z            integer,
                x            integer,
                y            integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql
            AS $$
            DECLARE
                v_pid     uuid;
                v_wsid    uuid;
                v         bigint;
                tile_bbox geometry;
                simp_tol  double precision;
            BEGIN
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                BEGIN
                    v_wsid := NULLIF(query_params->>'workspace_id', '')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'silver.pg_formations_by_project: workspace_id in query_params is not a valid UUID';
                END;
                IF v_wsid IS NULL THEN
                    RAISE EXCEPTION 'silver.pg_formations_by_project: workspace_id is required in query_params (tenant-scoped tile source)';
                END IF;

                PERFORM set_config('app.workspace_id', v_wsid::text, true);

                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid
                  AND p.workspace_id = v_wsid;

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
                        f.project_id        AS project_id,
                        f.formation_code    AS formation_code,
                        f.formation_name    AS formation_name,
                        f.age_period        AS age_period,
                        f.age_ma_lower      AS age_ma_lower,
                        f.age_ma_upper      AS age_ma_upper,
                        f.lithology_primary AS lithology_primary,
                        ST_AsMVTGeom(
                            ST_SimplifyPreserveTopology(
                                ST_Transform(f.geom, 3857), simp_tol
                            ),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM silver.geological_formations f
                    WHERE f.project_id = v_pid
                      AND f.workspace_id = v_wsid
                      AND ST_Intersects(ST_Transform(f.geom, 3857), tile_bbox)
                    ORDER BY f.id
                )
                SELECT
                    ST_AsMVT(tile, 'formations', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text || '|' || v_wsid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        // silver.pg_historic_workings_by_project
        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_historic_workings_by_project(
                z            integer,
                x            integer,
                y            integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql
            AS $$
            DECLARE
                v_pid     uuid;
                v_wsid    uuid;
                v         bigint;
                tile_bbox geometry;
            BEGIN
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                BEGIN
                    v_wsid := NULLIF(query_params->>'workspace_id', '')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'silver.pg_historic_workings_by_project: workspace_id in query_params is not a valid UUID';
                END;
                IF v_wsid IS NULL THEN
                    RAISE EXCEPTION 'silver.pg_historic_workings_by_project: workspace_id is required in query_params (tenant-scoped tile source)';
                END IF;

                PERFORM set_config('app.workspace_id', v_wsid::text, true);

                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid
                  AND p.workspace_id = v_wsid;

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
                      AND hw.workspace_id = v_wsid
                      AND ST_Intersects(ST_Transform(hw.geom, 3857), tile_bbox)
                    ORDER BY hw.id
                )
                SELECT
                    ST_AsMVT(tile, 'historic_workings', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text || '|' || v_wsid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        // silver.pg_geochem_by_project
        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_geochem_by_project(
                z            integer,
                x            integer,
                y            integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql
            AS $$
            DECLARE
                v_pid     uuid;
                v_wsid    uuid;
                v         bigint;
                tile_bbox geometry;
            BEGIN
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                BEGIN
                    v_wsid := NULLIF(query_params->>'workspace_id', '')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'silver.pg_geochem_by_project: workspace_id in query_params is not a valid UUID';
                END;
                IF v_wsid IS NULL THEN
                    RAISE EXCEPTION 'silver.pg_geochem_by_project: workspace_id is required in query_params (tenant-scoped tile source)';
                END IF;

                PERFORM set_config('app.workspace_id', v_wsid::text, true);

                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid
                  AND p.workspace_id = v_wsid;

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
                      AND gc.workspace_id = v_wsid
                      AND gc.geom IS NOT NULL
                      AND ST_Intersects(ST_Transform(gc.geom, 3857), tile_bbox)
                    ORDER BY gc.geochem_id
                )
                SELECT
                    ST_AsMVT(tile, 'geochem', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text || '|' || v_wsid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        // silver.pg_cross_section_lines_by_project
        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_cross_section_lines_by_project(
                z integer,
                x integer,
                y integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql
            AS $$
            DECLARE
                v_pid     uuid;
                v_wsid    uuid;
                v         bigint;
                tile_bbox geometry;
                tolerance double precision;
            BEGIN
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                BEGIN
                    v_wsid := NULLIF(query_params->>'workspace_id', '')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'silver.pg_cross_section_lines_by_project: workspace_id in query_params is not a valid UUID';
                END;
                IF v_wsid IS NULL THEN
                    RAISE EXCEPTION 'silver.pg_cross_section_lines_by_project: workspace_id is required in query_params (tenant-scoped tile source)';
                END IF;

                PERFORM set_config('app.workspace_id', v_wsid::text, true);

                SELECT p.data_version INTO v
                  FROM silver.projects p
                 WHERE p.project_id = v_pid
                   AND p.workspace_id = v_wsid;

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
                    WHERE p.project_id = v_pid
                      AND p.workspace_id = v_wsid
                      AND ST_Intersects(
                            ST_Transform(p.section_line_geom, 3857),
                            tile_bbox
                        )
                    ORDER BY p.panel_id
                )
                SELECT
                    ST_AsMVT(tile, 'cross_section_lines', 4096, 'geom') AS mvt,
                    md5(
                        v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text || '|' || v_wsid::text
                    ) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);
    }
};
