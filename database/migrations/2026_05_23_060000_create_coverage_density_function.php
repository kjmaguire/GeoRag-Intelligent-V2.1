<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * CC-03 Item 5 — silver.coverage_density() PostGIS function.
 *
 * Buckets project records (collars / reports / spatial_features) into a
 * hexagonal grid sized at `cell_size_m` metres and returns one row per
 * cell with a count + the cell polygon. The FastAPI /coverage/density
 * endpoint wraps the rows into a GeoJSON FeatureCollection for MapLibre.
 *
 * Why a hex grid:
 *   - hex cells have uniform neighbour distance (unlike square grids),
 *     which avoids the directional bias artefacts that mislead
 *     geologists looking for "trend" — important because the explicit
 *     UX goal (Anna's request) is to communicate that data absence ≠
 *     mineralization absence.
 *   - PostGIS ST_HexagonGrid was added in 3.1 and is available in 3.6.
 *
 * Why server-side aggregation:
 *   - A project with 50k collars + 10k samples is too many features for
 *     a MapLibre layer to render directly. Aggregating to ~1k cells
 *     keeps the response small (<100 KB GeoJSON) and the render fast.
 *
 * Anti-bias UX contract (per CC-03 Item 5 spec — Anna's explicit ask):
 *   - Every cell carries a `bias_warning` boolean: TRUE when count < 3.
 *   - The frontend renders sparse cells with a dashed border + the
 *     literal label "X reports in this area — results may reflect
 *     historical exploration bias" on hover. The function returns the
 *     count; the warning copy is generated client-side from the count.
 *
 * Workspace tenancy: the function takes project_id as input; it does
 * NOT filter by workspace_id because the caller (FastAPI router) has
 * already set the RLS GUC via SET LOCAL app.workspace_id. silver.*
 * tables RLS will enforce per-row visibility automatically.
 *
 * SQLite — gated on Postgres (PostGIS + hex grid).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // The function returns (cell_polygon, count, bias_warning) so the
        // caller can build a GeoJSON FeatureCollection without a second
        // pass. cell_size_m must be one of 500 / 1000 / 5000 / 10000 to
        // avoid degenerate hex grids that overload the renderer.
        DB::statement(<<<'SQL'
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

        // Grant execute to the application role so the FastAPI service
        // (asyncpg connection as georag_app) can call it without su.
        DB::statement('GRANT EXECUTE ON FUNCTION silver.coverage_density(uuid, text, integer) TO georag_app');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('DROP FUNCTION IF EXISTS silver.coverage_density(uuid, text, integer)');
    }
};
