<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * silver.pg_collars_by_project reads geom_4326, not the 32613 `geom` (GIS-21).
 *
 * WHY
 *   silver.collars.geom is geometry(Point, 32613) — every collar on earth is
 *   stored in UTM 13N, which breaks the platform's EPSG:4326-at-rest rule.
 *   Retiring the column is a §04e schema change and Kyle's call; this does
 *   not touch it. What it does is stop the collar tile source depending on
 *   it: geom_4326 is transformed straight from each collar's SOURCE CRS at
 *   insert, so it is the one position every writer agrees on, and it is what
 *   drill traces, the agent tools and promotion already read.
 *
 *   PROJ's extended Transverse Mercator round-trips 32613 correctly far
 *   outside the zone (audited for Alaska, WA, PNG, Norway), so the tiles do
 *   not move for existing data; the change removes a needless double
 *   transform (source -> 32613 -> 3857) and makes the source ready for
 *   `geom` to be retired.
 *
 *   The tile predicate is now `geom_4326 && <tile envelope in 4326>`, which
 *   can use idx_collars_geom_4326; `ST_Intersects(ST_Transform(geom, 3857),
 *   tile)` could use no index at all. A Web Mercator tile envelope is an
 *   axis-aligned lon/lat rectangle, so transforming the ENVELOPE is exact.
 *
 * CRS at every hop: source -> geom_4326 (at ingest) -> 3857 here, for MVT.
 *
 * Everything else — signature, workspace scoping and the raise on a missing
 * workspace_id, published properties, ORDER BY, etag — is identical to
 * 2026_09_16_120000.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_collars_by_project(
                z integer,
                x integer,
                y integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql
            AS $$
            DECLARE
                v_pid       uuid;
                v_wsid      uuid;
                v           bigint;
                tile_bbox   geometry;
                tile_4326   geometry;
            BEGIN
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                BEGIN
                    v_wsid := NULLIF(query_params->>'workspace_id', '')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'silver.pg_collars_by_project: workspace_id in query_params is not a valid UUID';
                END;
                IF v_wsid IS NULL THEN
                    RAISE EXCEPTION 'silver.pg_collars_by_project: workspace_id is required in query_params (tenant-scoped tile source)';
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
                tile_4326 := ST_Transform(tile_bbox, 4326);

                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(c.collar_id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        c.project_id                            AS project_id,
                        c.hole_id                               AS hole_id,
                        c.azimuth                               AS collar_azimuth,
                        c.dip                                   AS collar_dip,
                        c.total_depth                           AS total_depth_m,
                        c.spatial_uncertainty_m                 AS spatial_uncertainty_m,
                        c.crs_confidence                        AS crs_confidence,
                        c.georef_method                         AS georef_method,
                        ST_Y(c.geom_4326)::real                 AS _lat,
                        ST_AsMVTGeom(
                            ST_Transform(c.geom_4326, 3857),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM silver.collars c
                    WHERE c.project_id = v_pid
                      AND c.workspace_id = v_wsid
                      AND c.geom_4326 IS NOT NULL
                      AND c.geom_4326 && tile_4326
                    ORDER BY c.collar_id
                )
                SELECT
                    ST_AsMVT(tile, 'collars', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text || '|' || v_wsid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_collars_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.collars.geom_4326 (EPSG:4326→3857; GIS-21 2026-09-29 — no longer the 32613 geom column). Requires workspace_id in query_params (raises if missing/invalid); arms app.workspace_id via set_config and filters explicitly on workspace_id as defense-in-depth alongside RLS. Publishes spatial_uncertainty_m + crs_confidence + georef_method + _lat. ORDER BY collar_id for deterministic ST_AsMVT.'");
    }

    public function down(): void
    {
        // Deliberately no-op, like 2026_09_16_120000: the previous body is
        // functionally equivalent for existing data (extended TM round-trips)
        // and a revert should be a new, reviewed migration.
    }
};
