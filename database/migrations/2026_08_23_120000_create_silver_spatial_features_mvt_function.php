<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * silver.pg_spatial_features_by_project — the 8th project-scoped MVT
 * function-source, and the first one that puts IMPORTED vector data on the map.
 *
 * Why this exists
 * ───────────────
 * silver.spatial_features is the landing table for every generic GIS vector
 * import (.shp / .gpkg / .gdb / .dxf / .geojson / MapInfo), written by
 * app/hatchet_workflows/ingest_spatial.py. Until now it backed NO map layer:
 * the seven registered layers each read a different dedicated table (collars,
 * drill_traces, seismic_surveys, project_boundaries, geological_formations,
 * historic_workings, geochemistry), and an imported shapefile surfaced in the
 * UI only as a coverage-density hexbin — an aggregate count, not the geometry.
 *
 * Contract (identical to the existing seven — see
 * 2026_04_22_130000_create_silver_mvt_functions.php and
 * 2026_04_22_140000_create_silver_boundary_formation_working_geochem.php):
 *
 *   §05d signature   RETURNS TABLE (mvt bytea, etag_hash text)
 *   arguments        (z integer, x integer, y integer, query_params json)
 *   scoping          query_params->>'project_id'; NULL project_id or an
 *                    unknown project returns (NULL, NULL) rather than raising
 *   workspace        delegated to the RLS policy
 *                    `spatial_features_workspace_isolation` on the table, the
 *                    same way all seven existing functions delegate theirs —
 *                    the functions are SECURITY INVOKER, so the caller's
 *                    `app.workspace_id` GUC filters the rows
 *   etag             md5(silver.projects.data_version|z|x|y|project_id)
 *   volatility       STABLE PARALLEL SAFE
 *
 * ── DECISION: MIXED GEOMETRY IS SPLIT INSIDE THE FUNCTION ───────────────────
 *
 * Unlike every existing layer, silver.spatial_features is deliberately mixed
 * geometry — its column is geometry(Geometry,4326) and one import can land
 * points, lines and polygons together. A single MapLibre layer has exactly one
 * `type` (circle | line | fill) and cannot paint all three.
 *
 * The two options were (a) emit ONE MVT layer and give the frontend three
 * MapLibre layers filtered on ['==', ['geometry-type'], …], or (b) split by
 * geometry dimension here and emit THREE named MVT layers. We chose (b):
 *
 *   - The split is a property of the DATA, not of one client. Any future tile
 *     consumer gets correctly-typed layers without re-deriving the rule.
 *   - Simplification differs per dimension: points must NOT be simplified,
 *     lines and polygons must be. One MVT layer would force one policy.
 *   - MapLibre's `geometry-type` filter is evaluated per feature at render
 *     time on every frame; three pre-split source-layers cost nothing at
 *     render time.
 *
 * A single MVT tile may legally carry several layers (`repeated Layer layers`
 * = field 3 of the Tile message), and concatenating the bytea returned by
 * three ST_AsMVT calls is exactly that repeated field, so the three layers ride
 * in ONE tile response and cost ONE HTTP request. Verified by decoding the
 * produced protobuf: 3 top-level layer records, buffer fully consumed, MVT
 * spec version 2 on each.
 *
 * The three ST_AsMVT literals — these are the source-layer names the frontend
 * registry (resources/js/lib/mvtLayers.ts) MUST match exactly:
 *
 *     imported_points     ST_Dimension(geom) = 0   (POINT / MULTIPOINT)
 *     imported_lines      ST_Dimension(geom) = 1   (LINESTRING / MULTILINESTRING)
 *     imported_polygons   ST_Dimension(geom) = 2   (POLYGON / MULTIPOLYGON)
 *
 * ST_Dimension is used rather than GeometryType() because it collapses the
 * single/multi distinction for free and returns the maximum dimension of a
 * GEOMETRYCOLLECTION instead of falling through unclassified.
 *
 * ── TILE PROPERTIES ─────────────────────────────────────────────────────────
 *
 * feature_type (the twelve-value CHECK vocabulary from
 * 2026_05_22_010000_extend_silver_spatial_features.php) and source_layer (the
 * layer name inside the .gpkg/.gdb, or the shapefile stem) are exposed so the
 * layer can be colour-styled and filtered per import. Also carried:
 * feature_name, feature_role, source_file, source_crs, crs_epsg_native,
 * crs_confidence, georef_method, confidence — the georeferencing-provenance
 * set a geologist needs to judge whether an imported outline can be trusted.
 *
 * ── BBOX PREDICATE ──────────────────────────────────────────────────────────
 *
 * The existing seven write `ST_Intersects(ST_Transform(t.geom, 3857), bbox)`,
 * which wraps the indexed column in a function and therefore cannot use the
 * GIST index. Those tables are small; silver.spatial_features is not — one
 * shapefile import routinely lands tens of thousands of features. This
 * function instead transforms the TILE ENVELOPE into 4326 once and filters
 * `sf.geom && bbox_4326 AND ST_Intersects(sf.geom, bbox_4326)`, leaving the
 * indexed column bare so idx_spatial_features_geom is usable. The output is
 * identical; only the plan differs.
 *
 * ── GRANTS ──────────────────────────────────────────────────────────────────
 *
 * 2026_04_22_150000_grant_martin_readonly_select.php installed ALTER DEFAULT
 * PRIVILEGES for FUTURE silver tables, but silver.spatial_features was created
 * back in 2026_04_10_120100 and so was never covered. Measured on the live
 * local database before this migration:
 *   has_table_privilege('martin_readonly','silver.spatial_features','SELECT') = f
 * Without the explicit grant below the function executes and returns an empty
 * tile — the exact silent failure mode Chunk 8.3 was written to fix.
 *
 * Idempotent: CREATE OR REPLACE FUNCTION + idempotent GRANTs.
 * SQLite — gated on Postgres (PostGIS-only).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_spatial_features_by_project(
                z            integer,
                x            integer,
                y            integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            AS $$
            DECLARE
                v_pid     uuid;
                v         bigint;
                tile_bbox geometry;   -- EPSG:3857, the MVT output frame
                bbox_4326 geometry;   -- same envelope in 4326, for the indexed prefilter
                tolerance double precision;
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
                bbox_4326 := ST_Transform(tile_bbox, 4326);
                tolerance := GREATEST(0.5, 156543.034 / (2 ^ z) * 0.5);

                RETURN QUERY
                WITH src AS (
                    SELECT
                        (hashtext(sf.feature_id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        sf.project_id               AS project_id,
                        sf.feature_type             AS feature_type,
                        sf.feature_name             AS feature_name,
                        sf.feature_role             AS feature_role,
                        sf.source_layer             AS source_layer,
                        sf.source_file              AS source_file,
                        sf.source_crs               AS source_crs,
                        sf.crs_epsg_native          AS crs_epsg_native,
                        sf.crs_confidence           AS crs_confidence,
                        sf.georef_method            AS georef_method,
                        sf.confidence               AS confidence,
                        ST_Dimension(sf.geom)       AS geom_dim,
                        ST_Transform(sf.geom, 3857) AS geom_3857
                    FROM silver.spatial_features sf
                    WHERE sf.project_id = v_pid
                      AND sf.geom IS NOT NULL
                      AND sf.geom && bbox_4326
                      AND ST_Intersects(sf.geom, bbox_4326)
                ),
                pts AS (
                    SELECT feature_id, project_id, feature_type, feature_name, feature_role,
                           source_layer, source_file, source_crs, crs_epsg_native,
                           crs_confidence, georef_method, confidence,
                           ST_AsMVTGeom(geom_3857, tile_bbox, 4096, 64, true) AS geom
                      FROM src
                     WHERE geom_dim = 0
                ),
                lns AS (
                    SELECT feature_id, project_id, feature_type, feature_name, feature_role,
                           source_layer, source_file, source_crs, crs_epsg_native,
                           crs_confidence, georef_method, confidence,
                           ST_AsMVTGeom(
                               ST_SimplifyPreserveTopology(geom_3857, tolerance),
                               tile_bbox, 4096, 64, true
                           ) AS geom
                      FROM src
                     WHERE geom_dim = 1
                ),
                plys AS (
                    SELECT feature_id, project_id, feature_type, feature_name, feature_role,
                           source_layer, source_file, source_crs, crs_epsg_native,
                           crs_confidence, georef_method, confidence,
                           ST_AsMVTGeom(
                               ST_SimplifyPreserveTopology(geom_3857, tolerance),
                               tile_bbox, 4096, 64, true
                           ) AS geom
                      FROM src
                     WHERE geom_dim = 2
                )
                -- One tile, three layers. `repeated Layer layers = 3` in the MVT
                -- Tile message means concatenating the three encodings IS the
                -- repeated field; an empty ST_AsMVT contributes zero bytes.
                SELECT
                    COALESCE((SELECT ST_AsMVT(t, 'imported_points', 4096, 'geom')
                                FROM (SELECT * FROM pts WHERE geom IS NOT NULL) t), ''::bytea)
                 || COALESCE((SELECT ST_AsMVT(t, 'imported_lines', 4096, 'geom')
                                FROM (SELECT * FROM lns WHERE geom IS NOT NULL) t), ''::bytea)
                 || COALESCE((SELECT ST_AsMVT(t, 'imported_polygons', 4096, 'geom')
                                FROM (SELECT * FROM plys WHERE geom IS NOT NULL) t), ''::bytea)
                        AS mvt,
                    md5(
                        v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text
                    ) AS etag_hash;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_spatial_features_by_project(integer, integer, integer, json) IS
            'MVT function-source. §05d signature: RETURNS TABLE(mvt bytea, etag_hash text). Source: silver.spatial_features (mixed geometry, EPSG:4326). Emits THREE MVT layers in one tile — imported_points / imported_lines / imported_polygons — split on ST_Dimension, because one MapLibre layer cannot paint points, lines and polygons. Points are never simplified; lines and polygons get zoom-aware ST_SimplifyPreserveTopology. Properties: feature_type, source_layer, feature_name, feature_role, source_file, source_crs, crs_epsg_native, crs_confidence, georef_method, confidence. ETag = md5(data_version|z|x|y|project_id). Requirement 8, 2026-08-23.'");

        DB::statement('GRANT EXECUTE ON FUNCTION silver.pg_spatial_features_by_project(integer, integer, integer, json) TO martin_readonly;');

        // silver.spatial_features predates the ALTER DEFAULT PRIVILEGES installed
        // by 2026_04_22_150000, so it never received the martin_readonly SELECT
        // grant. Without this the function returns empty tiles, not an error.
        DB::statement('GRANT SELECT ON silver.spatial_features TO martin_readonly;');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('REVOKE SELECT ON silver.spatial_features FROM martin_readonly;');
        DB::statement('DROP FUNCTION IF EXISTS silver.pg_spatial_features_by_project(integer, integer, integer, json)');
    }
};
