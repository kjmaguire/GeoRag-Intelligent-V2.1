-- =============================================================================
-- §6.13 — silver.density_choropleth_h3 MVT function for Martin.
--
-- Reads gold.h3_density_mineral and emits MVT bytes per Martin's
-- function-source contract: RETURNS TABLE(mvt bytea, etag_hash text).
-- Picks the appropriate h3 resolution based on the zoom level:
--   z ≤ 5   → resolution 5  (continental, ≈252 km hex)
--   z ≤ 10  → resolution 7  (regional,    ≈36 km hex)
--   z > 10  → resolution 9  (project,     ≈5 km hex)
-- This matches the {5, 7, 9} locked in master_plan_section6_kickoff.md.
--
-- Each MVT feature carries:
--   - h3_index (as text — h3index doesn't survive MVT serialization natively)
--   - commodity_code
--   - resolution
--   - occurrence_count + drillhole_count
--   - count_total (derived for the renderer's color ramp)
--
-- Idempotent (CREATE OR REPLACE).
-- =============================================================================

BEGIN;

CREATE OR REPLACE FUNCTION silver.density_choropleth_h3(
    z integer, x integer, y integer,
    query_params json DEFAULT '{}'::json
)
RETURNS TABLE(mvt bytea, etag_hash text)
LANGUAGE plpgsql
STABLE PARALLEL SAFE
AS $function$
    DECLARE
        tile_bbox  geometry;
        v          bigint;
        target_resolution smallint;
        commodity_filter text;
    BEGIN
        tile_bbox := ST_TileEnvelope(z, x, y);

        -- Zoom-adaptive h3 resolution. Kept inline so the function is
        -- self-contained — no external lookup table needed.
        target_resolution := CASE
            WHEN z <= 5  THEN 5::smallint
            WHEN z <= 10 THEN 7::smallint
            ELSE              9::smallint
        END;

        -- Optional commodity filter from the URL query string.
        -- e.g. /tiles/density_choropleth_h3/3/2/1?commodity=au
        commodity_filter := LOWER(NULLIF(query_params->>'commodity', ''));

        -- etag_hash basis: latest computed_at across the relevant
        -- resolution + the tile coords + commodity filter. Bumps the
        -- ETag whenever the nightly Dagster materialisation refreshes.
        SELECT COALESCE(EXTRACT(EPOCH FROM MAX(computed_at))::bigint, 0)
          INTO v
          FROM gold.h3_density_mineral
         WHERE resolution = target_resolution;

        RETURN QUERY
        WITH tile AS (
            SELECT
                d.h3_index::text                       AS h3_index,
                d.commodity_code,
                d.resolution::int                      AS resolution,
                d.occurrence_count,
                d.drillhole_count,
                (d.occurrence_count + d.drillhole_count) AS count_total,
                ST_AsMVTGeom(
                    ST_Transform(
                        silver.h3_cell_to_boundary_geometry(d.h3_index),
                        3857
                    ),
                    tile_bbox, 4096, 64, true
                ) AS geom
            FROM gold.h3_density_mineral d
            WHERE d.resolution = target_resolution
              AND (commodity_filter IS NULL OR d.commodity_code = commodity_filter)
              AND ST_Intersects(
                    ST_Transform(
                        silver.h3_cell_to_boundary_geometry(d.h3_index),
                        3857
                    ),
                    tile_bbox
                  )
            ORDER BY count_total DESC
        )
        SELECT
            ST_AsMVT(tile, 'density_choropleth_h3', 4096, 'geom') AS mvt,
            md5(
                v::text || '|' || z::text || '|' || x::text || '|' || y::text
                || '|' || COALESCE(commodity_filter, 'all')
            ) AS etag_hash
        FROM tile;
    END;
    $function$;

COMMENT ON FUNCTION silver.density_choropleth_h3(integer, integer, integer, json) IS
    '§6.13 — MVT source for the density choropleth layer. Reads '
    'gold.h3_density_mineral; selects h3 resolution by zoom band '
    '(z≤5→5, z≤10→7, else→9). Optional ?commodity= filter.';

-- Grant execute to georag_app for completeness (Martin connects as a
-- separate role per docker compose).
GRANT EXECUTE ON FUNCTION silver.density_choropleth_h3(integer, integer, integer, json) TO georag_app;

COMMIT;
