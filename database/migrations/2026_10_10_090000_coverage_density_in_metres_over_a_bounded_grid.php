<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * silver.coverage_density: cells are p_cell_size_m METRES across, records are
 * counted wherever they intersect a cell, and the grid is bounded
 * (GIS audit 2026-10, finding 14; database audit 2026-10).
 *
 * WHAT WAS WRONG (2026_05_23_060000)
 *
 * 1. Cells were sized in EPSG:3857 units. A Web Mercator "metre" is only a
 *    ground metre at the equator; at latitude phi it is cos(phi) of one. The
 *    1 km hexagon the UI asks for was drawn 531 m across at 58 N, and the
 *    5 km one 2674 m (measured on a 20k-collar project at 106 W, 58 N). The
 *    cell size printed in the legend was therefore wrong everywhere a
 *    Canadian or Australian exploration project actually sits.
 *
 * 2. Records were counted with ST_Contains(cell, record), which is true only
 *    when the WHOLE record lies inside ONE cell. A report outline or a mapped
 *    fault, which is bigger than any cell, was counted in none: on a seeded
 *    project two outlines (18 x 15 km and 12 x 11 km) gave a 'reports' layer
 *    with no cells at all, and a 63 km fault was counted in no cell of the
 *    'spatial_features' layer (it is counted in each cell it crosses now).
 *    For a layer whose purpose is to say "this much of the ground has been
 *    looked at" that is an undercount presented as an absence, and absence
 *    is exactly what the layer's bias_warning contract says not to imply.
 *
 * 3. The grid was unbounded. The extent is ST_Extent over every row of the
 *    project, so one mis-located record (lon/lat 0,0) stretched it across a
 *    hemisphere and the function tried to build millions of hexagons; and each
 *    hexagon was tested against every record with no index (62k cells x 20k
 *    collars took 185 s; georag_app's statement_timeout is 300 s), so a single
 *    request could pin a connection for five minutes.
 *
 * WHAT IT DOES NOW
 *
 * CRS at every hop: silver.collars.geom_4326 / silver.reports.geom /
 * silver.spatial_features.geom (EPSG:4326) -> the UTM zone of the centre of
 * the records' lon/lat extent (326xx north, 327xx south; metres) -> a hexagon
 * lattice whose EDGE is p_cell_size_m / 2 (so the long axis is p_cell_size_m)
 * -> cell polygons transformed back to EPSG:4326 for the response. One local
 * frame keeps the cell size within about 0.1 % of nominal across a project
 * (the UTM scale factor is 0.9996 on the central meridian and at most about
 * 1.001 at a zone's edge; measured 500.18 m for a 500 m cell at 106 W, 58 N
 * and 1000.17 m for a 1 km cell at 33 S); it does not model the earth as one
 * flat sheet, so the extent is limited (below).
 *
 * A record counts once in every cell it intersects (ST_Intersects): a point in
 * its cell, a line in each cell it crosses, a polygon in each cell it covers.
 * Invalid geometries (self-intersecting hand-digitised outlines) are repaired
 * with ST_MakeValid first, because intersecting a bow-tie can raise a GEOS
 * TopologyException where ST_Contains never did.
 *
 * The hexagon x record join goes through a square bucket key (bucket side =
 * the cell's long axis, which is also its bounding-box width): a record and a
 * cell can only intersect if their bounding boxes share a bucket, so the
 * exact test runs on a handful of candidate pairs per record instead of on
 * every pair. Same answer as the all-pairs join (checked against it on 20k
 * points at 5 km and 10 km, and on lines and polygons), 1.6 s instead of 185 s.
 *
 * The request is REFUSED, with SQLSTATE 54000 (program_limit_exceeded) and a
 * hint, when the extent would need more than 200 000 cells, is wider than
 * 2 000 km in the local frame, or spans more than 180 degrees of longitude.
 * The span limits are the ones that protect the cell size: at coarse cells the
 * cell count alone would allow a continental extent, far enough from one UTM
 * central meridian for the scale to be off by several percent; and records on
 * both sides of the 180th meridian put the extent's centre, and so the UTM
 * zone, on the far side of the world from the data. An empty result is NOT
 * used for a refusal: an empty map reads as "no coverage here", which is the
 * misreading the layer exists to prevent. The FastAPI route turns the error
 * into HTTP 422.
 *
 * Unchanged: the signature, the return columns, bias_warning = (count < 3),
 * "cells with count 0 are dropped", SECURITY INVOKER (so the RLS workspace
 * GUC the caller set still decides which rows are visible) and the grant to
 * georag_app, which CREATE OR REPLACE keeps.
 *
 * down() restores the 2026_05_23_060000 definition, verbatim.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.coverage_density(
                p_project_id  uuid,
                p_kind        text,
                p_cell_size_m integer DEFAULT 1000
            )
            RETURNS TABLE (
                cell_polygon  geometry(Polygon, 4326),
                record_count  integer,
                bias_warning  boolean
            )
            LANGUAGE plpgsql
            STABLE
            AS $$
            DECLARE
                c_max_cells  CONSTANT integer          := 200000;
                c_max_span_m CONSTANT double precision := 2000000;
                v_edge       double precision          := p_cell_size_m / 2.0;
                v_geoms      geometry[];
                v_box        box2d;
                v_srid       integer;
                v_width_m    double precision;
                v_height_m   double precision;
                v_cells      double precision;
            BEGIN
                IF p_kind NOT IN ('collars', 'reports', 'spatial_features') THEN
                    RAISE EXCEPTION 'coverage_density: p_kind must be one of collars / reports / spatial_features (got %)', p_kind
                      USING ERRCODE = 'invalid_parameter_value';
                END IF;

                IF p_cell_size_m NOT IN (500, 1000, 5000, 10000) THEN
                    RAISE EXCEPTION 'coverage_density: p_cell_size_m must be one of 500/1000/5000/10000 (got %)', p_cell_size_m
                      USING ERRCODE = 'invalid_parameter_value';
                END IF;

                -- The project's records, EPSG:4326. The ONE read of the three
                -- tables: the extent and the counts below both come from this
                -- array, so the grid always covers exactly what is counted.
                SELECT array_agg(ST_MakeValid(s.g))
                  INTO v_geoms
                  FROM (
                        SELECT c.geom_4326 AS g
                          FROM silver.collars c
                         WHERE p_kind = 'collars'
                           AND c.project_id = p_project_id
                           AND c.geom_4326 IS NOT NULL
                        UNION ALL
                        SELECT r.geom
                          FROM silver.reports r
                         WHERE p_kind = 'reports'
                           AND r.project_id = p_project_id
                           AND r.geom IS NOT NULL
                        UNION ALL
                        SELECT sf.geom
                          FROM silver.spatial_features sf
                         WHERE p_kind = 'spatial_features'
                           AND sf.project_id = p_project_id
                           AND sf.geom IS NOT NULL
                       ) s
                 WHERE NOT ST_IsEmpty(s.g);

                -- Empty project: return no rows (the caller surfaces "no data").
                IF v_geoms IS NULL THEN
                    RETURN;
                END IF;

                -- Local frame: the UTM zone of the centre of the lon/lat extent.
                -- A lon/lat extent wider than half the world is a record on the
                -- far side of the 180th meridian (or a mis-located one); its
                -- centre is nowhere near the data, so there is no sensible zone.
                SELECT ST_Extent(g) INTO v_box FROM unnest(v_geoms) AS g;
                IF ST_XMax(v_box) - ST_XMin(v_box) > 180.0 THEN
                    RAISE EXCEPTION 'coverage_density: the % of this project span % degrees of longitude, which cannot be drawn on one local grid',
                        p_kind, round((ST_XMax(v_box) - ST_XMin(v_box))::numeric, 1)
                      USING ERRCODE = 'program_limit_exceeded',
                            HINT = 'Look for a mis-located record (a point at lon/lat 0,0 is the usual one). A project that crosses the 180th meridian is not supported by this layer.';
                END IF;
                v_srid := CASE WHEN (ST_YMin(v_box) + ST_YMax(v_box)) / 2.0 >= 0 THEN 32600 ELSE 32700 END
                          + LEAST(60, GREATEST(1,
                              floor(((ST_XMin(v_box) + ST_XMax(v_box)) / 2.0 + 180.0) / 6.0)::integer + 1));

                -- The extent in metres, and how big a grid it would need. A
                -- comparison with NaN or Infinity is true, so a degenerate
                -- extent is refused rather than iterated.
                SELECT ST_Extent(ST_Transform(g, v_srid)) INTO v_box FROM unnest(v_geoms) AS g;
                v_width_m  := ST_XMax(v_box) - ST_XMin(v_box);
                v_height_m := ST_YMax(v_box) - ST_YMin(v_box);
                v_cells    := (floor(v_width_m  / (1.5 * v_edge)) + 2)
                            * (floor(v_height_m / (sqrt(3.0::double precision) * v_edge)) + 2);

                IF v_cells > c_max_cells OR v_width_m > c_max_span_m OR v_height_m > c_max_span_m THEN
                    RAISE EXCEPTION 'coverage_density: the % of this project span about % x % km, which needs about % cells of % m; the limits are % cells and % km across',
                        p_kind, round(v_width_m / 1000.0), round(v_height_m / 1000.0), round(v_cells),
                        p_cell_size_m, c_max_cells, round(c_max_span_m / 1000.0)
                      USING ERRCODE = 'program_limit_exceeded',
                            HINT = 'Choose a larger cell_size_m, or look for a mis-located record: a point at lon/lat 0,0, or data on both sides of the 180th meridian, stretches the extent across the map.';
                END IF;

                RETURN QUERY
                WITH recs AS (
                    SELECT t.rid, ST_Transform(t.g, v_srid) AS u
                      FROM unnest(v_geoms) WITH ORDINALITY AS t(g, rid)
                ),
                grid AS (
                    SELECT h.i, h.j, h.geom
                      FROM ST_HexagonGrid(
                               v_edge,
                               ST_MakeEnvelope(ST_XMin(v_box), ST_YMin(v_box), ST_XMax(v_box), ST_YMax(v_box), v_srid)
                           ) AS h
                ),
                -- Square bucket keys, side = the cell's long axis (= its bounding-box width).
                cell_keys AS (
                    SELECT g.i, g.j, kx, ky
                      FROM grid g
                     CROSS JOIN LATERAL generate_series(
                              floor(ST_XMin(g.geom) / p_cell_size_m)::bigint,
                              floor(ST_XMax(g.geom) / p_cell_size_m)::bigint) AS kx
                     CROSS JOIN LATERAL generate_series(
                              floor(ST_YMin(g.geom) / p_cell_size_m)::bigint,
                              floor(ST_YMax(g.geom) / p_cell_size_m)::bigint) AS ky
                ),
                rec_keys AS (
                    SELECT r.rid, kx, ky
                      FROM recs r
                     CROSS JOIN LATERAL generate_series(
                              floor(ST_XMin(r.u) / p_cell_size_m)::bigint,
                              floor(ST_XMax(r.u) / p_cell_size_m)::bigint) AS kx
                     CROSS JOIN LATERAL generate_series(
                              floor(ST_YMin(r.u) / p_cell_size_m)::bigint,
                              floor(ST_YMax(r.u) / p_cell_size_m)::bigint) AS ky
                ),
                candidates AS (
                    SELECT DISTINCT ck.i, ck.j, rk.rid
                      FROM cell_keys ck
                      JOIN rec_keys rk ON rk.kx = ck.kx AND rk.ky = ck.ky
                ),
                counted AS (
                    SELECT cand.i, cand.j, count(*) AS n
                      FROM candidates cand
                      JOIN grid g ON g.i = cand.i AND g.j = cand.j
                      JOIN recs r ON r.rid = cand.rid
                     WHERE ST_Intersects(g.geom, r.u)
                     GROUP BY cand.i, cand.j
                )
                SELECT ST_Transform(g.geom, 4326)::geometry(Polygon, 4326) AS cell_polygon,
                       c.n::integer                                        AS record_count,
                       (c.n < 3)::boolean                                  AS bias_warning
                  FROM counted c
                  JOIN grid g ON g.i = c.i AND g.j = c.j
                 ORDER BY c.n DESC, c.i, c.j;
            END;
            $$
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.coverage_density(uuid, text, integer) IS
            'CC-03 Item 5 — buckets project records into a hex grid for the coverage-density heatmap layer. Cells are p_cell_size_m metres across (long axis) in the local UTM zone of the records, returned in EPSG:4326. A record counts in every cell it intersects. Returns cells with count > 0 only; bias_warning=TRUE when count < 3 (sparse-coverage UX signal per Anna 2026-05-23). Raises 54000 program_limit_exceeded when the extent needs more than 200000 cells or is wider than 2000 km (GIS audit 2026-10).'");
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // 2026_05_23_060000 up(), verbatim.
        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.coverage_density(
                p_project_id  uuid,
                p_kind        text,
                p_cell_size_m integer DEFAULT 1000
            )
            RETURNS TABLE (
                cell_polygon  geometry(Polygon, 4326),
                record_count  integer,
                bias_warning  boolean
            )
            LANGUAGE plpgsql
            STABLE
            AS $$
            DECLARE
                v_extent    geometry;
                v_hex_size  numeric;
            BEGIN
                IF p_kind NOT IN ('collars', 'reports', 'spatial_features') THEN
                    RAISE EXCEPTION 'coverage_density: p_kind must be one of collars / reports / spatial_features (got %)', p_kind
                      USING ERRCODE = 'invalid_parameter_value';
                END IF;

                IF p_cell_size_m NOT IN (500, 1000, 5000, 10000) THEN
                    RAISE EXCEPTION 'coverage_density: p_cell_size_m must be one of 500/1000/5000/10000 (got %)', p_cell_size_m
                      USING ERRCODE = 'invalid_parameter_value';
                END IF;

                -- Compute the extent envelope of the project's records in
                -- web-mercator (3857) so the hex grid edges are in metres.
                IF p_kind = 'collars' THEN
                    SELECT ST_Transform(ST_SetSRID(ST_Extent(geom_4326)::geometry, 4326), 3857)
                      INTO v_extent
                      FROM silver.collars
                     WHERE project_id = p_project_id
                       AND geom_4326 IS NOT NULL;
                ELSIF p_kind = 'reports' THEN
                    SELECT ST_Transform(ST_SetSRID(ST_Extent(geom)::geometry, 4326), 3857)
                      INTO v_extent
                      FROM silver.reports
                     WHERE project_id = p_project_id
                       AND geom IS NOT NULL;
                ELSE  -- spatial_features
                    SELECT ST_Transform(ST_SetSRID(ST_Extent(geom)::geometry, 4326), 3857)
                      INTO v_extent
                      FROM silver.spatial_features
                     WHERE project_id = p_project_id
                       AND geom IS NOT NULL;
                END IF;

                -- Empty project — return no rows (caller surfaces "no data" panel).
                IF v_extent IS NULL OR ST_IsEmpty(v_extent) THEN
                    RETURN;
                END IF;

                -- ST_HexagonGrid takes edge-length in the SRS units; for 3857
                -- that's metres at the equator. Use cell_size as the long
                -- axis of the hexagon → edge length = cell_size / 2.
                v_hex_size := p_cell_size_m::numeric / 2.0;

                RETURN QUERY
                WITH grid AS (
                    SELECT (ST_HexagonGrid(v_hex_size, v_extent)).geom AS hex_3857
                ),
                points AS (
                    SELECT
                        CASE p_kind
                            WHEN 'collars' THEN ST_Transform(c.geom_4326, 3857)
                            ELSE NULL::geometry
                        END AS p_geom
                      FROM silver.collars c
                     WHERE p_kind = 'collars'
                       AND c.project_id = p_project_id
                       AND c.geom_4326 IS NOT NULL
                    UNION ALL
                    SELECT ST_Transform(r.geom, 3857) AS p_geom
                      FROM silver.reports r
                     WHERE p_kind = 'reports'
                       AND r.project_id = p_project_id
                       AND r.geom IS NOT NULL
                    UNION ALL
                    SELECT ST_Transform(sf.geom, 3857) AS p_geom
                      FROM silver.spatial_features sf
                     WHERE p_kind = 'spatial_features'
                       AND sf.project_id = p_project_id
                       AND sf.geom IS NOT NULL
                ),
                counted AS (
                    SELECT
                        g.hex_3857,
                        COUNT(p.p_geom) AS n
                      FROM grid g
                      LEFT JOIN points p
                        ON ST_Contains(g.hex_3857, p.p_geom)
                     GROUP BY g.hex_3857
                )
                SELECT
                    ST_Transform(c.hex_3857, 4326)::geometry(Polygon, 4326) AS cell_polygon,
                    c.n::integer                                            AS record_count,
                    (c.n < 3)::boolean                                      AS bias_warning
                  FROM counted c
                 WHERE c.n > 0  -- empty cells aren't interesting; drop them
                 ORDER BY c.n DESC;
            END;
            $$
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.coverage_density(uuid, text, integer) IS
            'CC-03 Item 5 — buckets project records into a hex grid for the coverage-density heatmap layer. Returns cells with count > 0 only. bias_warning=TRUE when count < 3 (sparse-coverage UX signal per Anna 2026-05-23).'");
    }
};
