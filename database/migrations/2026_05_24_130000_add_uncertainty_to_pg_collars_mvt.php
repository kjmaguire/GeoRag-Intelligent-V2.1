<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * CC-01 Item 2 follow-on — surface spatial uncertainty + CRS provenance in the
 * Martin MVT path for silver.pg_collars_by_project so the MapView default
 * (useMartinTiles=true) renders the uncertainty-rings layer.
 *
 * Adds four columns to the MVT properties:
 *   spatial_uncertainty_m  real             — ring radius in metres (filter key)
 *   crs_confidence         real             — 0-1 provenance score
 *   georef_method          varchar(16)      — declared/detected/assumed/manual/survey
 *   _lat                   real             — WGS84 latitude in degrees, used by
 *                                             the radius expression's cos(lat)
 *                                             Web-Mercator shrink correction (see
 *                                             UNCERTAINTY_RINGS_RADIUS_EXPR in
 *                                             resources/js/Components/MapView.tsx)
 *
 * Source schema (silver.collars) — three uncertainty columns added in
 * 2026_05_23_050000_add_spatial_uncertainty_to_collars_and_spatial_features.php.
 * Geometry is stored in EPSG:32613; we publish lat from ST_Transform(geom, 4326)
 * because the MVT geometry itself is in EPSG:3857 (pixel space).
 *
 * Preserves every other property of the previous version (2026_04_22_160000
 * fix migration): v_pid variable name, ORDER BY collar_id for deterministic
 * ST_AsMVT output, the EPSG:32613→3857 transform, and the §05d
 * (mvt, etag_hash) return contract.
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
                        (hashtext(c.collar_id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        c.project_id                            AS project_id,
                        c.hole_id                               AS hole_id,
                        c.azimuth                               AS collar_azimuth,
                        c.dip                                   AS collar_dip,
                        c.total_depth                           AS total_depth_m,
                        c.spatial_uncertainty_m                 AS spatial_uncertainty_m,
                        c.crs_confidence                        AS crs_confidence,
                        c.georef_method                         AS georef_method,
                        ST_Y(ST_Transform(c.geom, 4326))::real  AS _lat,
                        ST_AsMVTGeom(
                            ST_Transform(c.geom, 3857),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM silver.collars c
                    WHERE c.project_id = v_pid
                      AND ST_Intersects(ST_Transform(c.geom, 3857), tile_bbox)
                    ORDER BY c.collar_id
                )
                SELECT
                    ST_AsMVT(tile, 'collars', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_collars_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.collars (EPSG:32613→3857). Publishes spatial_uncertainty_m + crs_confidence + georef_method + _lat (WGS84) so MapView uncertainty-rings layer renders on MVT path. ORDER BY collar_id for deterministic ST_AsMVT (CC-01 Item 2 follow-on).'");
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // Restore the pre-uncertainty version (matches 2026_04_22_160000 body).
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
                        (hashtext(c.collar_id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        c.project_id                AS project_id,
                        c.hole_id                   AS hole_id,
                        c.azimuth                   AS collar_azimuth,
                        c.dip                       AS collar_dip,
                        c.total_depth               AS total_depth_m,
                        ST_AsMVTGeom(
                            ST_Transform(c.geom, 3857),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM silver.collars c
                    WHERE c.project_id = v_pid
                      AND ST_Intersects(ST_Transform(c.geom, 3857), tile_bbox)
                    ORDER BY c.collar_id
                )
                SELECT
                    ST_AsMVT(tile, 'collars', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);
    }
};
