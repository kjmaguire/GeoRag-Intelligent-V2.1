<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Module 8 Chunk 8.8 — fix MVT function bugs found during pgTAP execution.
 *
 * Bug 1: pg_mineral_occurrences_tiles references o.smdi_id but
 *        public_geo.v_pg_mineral_occurrences_mvt has no smdi_id column.
 *        The actual column is external_id (confirmed by \d on the view).
 *        This caused pgTAP test 09 assertions 9+ to cascade-fail via transaction abort.
 *
 * Bug 2: silver.pg_collars_by_project, pg_drill_traces_by_project, and
 *        pg_seismic_by_project lacked ORDER BY before ST_AsMVT. While not a
 *        correctness bug at runtime, ordering is required for deterministic
 *        snapshot test output (ST_AsMVT row ordering depends on scan order without
 *        explicit ORDER BY). Added ORDER BY id-equivalent (collar_id, trace_id,
 *        survey_id) to the CTE before ST_AsMVT aggregate in each function.
 *        Source: Deliverable B determinism check in Chunk 8.8 spec.
 *
 * Bug 3: The four 8.2b functions (pg_boundaries_by_project, pg_formations_by_project,
 *        pg_historic_workings_by_project, pg_geochem_by_project) also lack ORDER BY
 *        before ST_AsMVT. Fixed here for the same determinism reason.
 *
 * Only bug 1 was a runtime failure. Bugs 2-3 are correctness fixes for
 * deterministic snapshot testing — considered a real fix, not test-only.
 */
return new class extends Migration
{
    public function up(): void
    {
        // ════════════════════════════════════════════════════════════════════
        // BUG 1 — pg_mineral_occurrences_tiles: smdi_id → external_id
        // ════════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION public_geo.pg_mineral_occurrences_tiles(
                z integer,
                x integer,
                y integer,
                query_params json DEFAULT '{}'
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            AS $$
            DECLARE
                tile_bbox geometry;
                v         bigint;
            BEGIN
                tile_bbox := ST_TileEnvelope(z, x, y);
                SELECT COALESCE(EXTRACT(EPOCH FROM MAX(updated_at))::bigint, 0)
                INTO v FROM public_geo.jurisdictions;

                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(o.id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        o.jurisdiction_code,
                        o.source_id,
                        o.source_feature_id,
                        o.external_id,            -- was o.smdi_id (column does not exist)
                        o.name,
                        o.status,
                        o.primary_commodities,
                        o.associated_commodities,
                        o.commodity_grouping,
                        o.discovery_type,
                        o.production_flag,
                        o.source_url,
                        ST_AsMVTGeom(
                            ST_Transform(o.geom, 3857),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM public_geo.v_pg_mineral_occurrences_mvt o
                    WHERE ST_Intersects(ST_Transform(o.geom, 3857), tile_bbox)
                    ORDER BY o.id
                )
                SELECT
                    ST_AsMVT(tile, 'pg_mineral_occurrences', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement("COMMENT ON FUNCTION public_geo.pg_mineral_occurrences_tiles(integer, integer, integer, json) IS
            'Martin function-source wrapper for v_pg_mineral_occurrences_mvt. §05d signature. Bug fix (Chunk 8.8): smdi_id → external_id (actual view column). ORDER BY o.id added for deterministic ST_AsMVT output.'");

        // ════════════════════════════════════════════════════════════════════
        // BUG 2+3 — Add ORDER BY to all silver + pgeo MVT functions for
        //           deterministic ST_AsMVT output (snapshot test prerequisite)
        //
        // Pattern: each CTE ends with ORDER BY <pk_column> before ST_AsMVT.
        // ════════════════════════════════════════════════════════════════════

        // silver.pg_collars_by_project — ORDER BY c.collar_id
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

        DB::statement("COMMENT ON FUNCTION silver.pg_collars_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.collars (EPSG:32613→3857). ORDER BY collar_id added for deterministic ST_AsMVT (Chunk 8.8).'");

        // silver.pg_drill_traces_by_project — ORDER BY dt.trace_id
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
                      AND ST_Intersects(ST_Transform(dt.geom, 3857), tile_bbox)
                    ORDER BY dt.trace_id
                )
                SELECT
                    ST_AsMVT(tile, 'drill_traces', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_drill_traces_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.drill_traces JOIN silver.collars. ORDER BY trace_id added for deterministic ST_AsMVT (Chunk 8.8).'");

        // silver.pg_seismic_by_project — ORDER BY s.survey_id
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
                      AND s.bbox IS NOT NULL
                      AND ST_Intersects(ST_Transform(s.bbox, 3857), tile_bbox)
                    ORDER BY s.survey_id
                )
                SELECT
                    ST_AsMVT(tile, 'seismic', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_seismic_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.seismic_surveys.bbox (EPSG:4326→3857). ORDER BY survey_id added for deterministic ST_AsMVT (Chunk 8.8).'");

        // silver.pg_boundaries_by_project — ORDER BY b.id
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
                    ORDER BY b.id
                )
                SELECT
                    ST_AsMVT(tile, 'boundaries', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_boundaries_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.project_boundaries (MultiPolygon, EPSG:4326→3857). ORDER BY id added for deterministic ST_AsMVT (Chunk 8.8).'");

        // silver.pg_formations_by_project — ORDER BY f.id
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
                      AND ST_Intersects(ST_Transform(f.geom, 3857), tile_bbox)
                    ORDER BY f.id
                )
                SELECT
                    ST_AsMVT(tile, 'formations', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_formations_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.geological_formations (MultiPolygon, EPSG:4326→3857). ORDER BY id added for deterministic ST_AsMVT (Chunk 8.8).'");

        // silver.pg_historic_workings_by_project — ORDER BY hw.id
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
                    ORDER BY hw.id
                )
                SELECT
                    ST_AsMVT(tile, 'historic_workings', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_historic_workings_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.historic_workings (Point, EPSG:4326→3857). ORDER BY id added for deterministic ST_AsMVT (Chunk 8.8).'");

        // silver.pg_geochem_by_project — ORDER BY gc.geochem_id
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
                    ORDER BY gc.geochem_id
                )
                SELECT
                    ST_AsMVT(tile, 'geochem', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_geochem_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.geochemistry (Point EPSG:4326). ORDER BY geochem_id added for deterministic ST_AsMVT (Chunk 8.8).'");

        // Add ORDER BY to the remaining 7 PGEO wrapper functions for determinism
        // (pg_mineral_occurrences already fixed above with bug 1 fix)

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION public_geo.pg_mines_tiles(
                z integer,
                x integer,
                y integer,
                query_params json DEFAULT '{}'
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            AS $$
            DECLARE
                tile_bbox geometry;
                v         bigint;
            BEGIN
                tile_bbox := ST_TileEnvelope(z, x, y);
                SELECT COALESCE(EXTRACT(EPOCH FROM MAX(updated_at))::bigint, 0)
                INTO v FROM public_geo.jurisdictions;

                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(m.id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        m.jurisdiction_code,
                        m.source_id,
                        m.source_feature_id,
                        m.name,
                        m.status,
                        m.commodities,
                        m.commodity_grouping,
                        m.operator,
                        m.source_url,
                        ST_AsMVTGeom(
                            ST_Transform(m.geom, 3857),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM public_geo.v_pg_mines_mvt m
                    WHERE ST_Intersects(ST_Transform(m.geom, 3857), tile_bbox)
                    ORDER BY m.id
                )
                SELECT
                    ST_AsMVT(tile, 'pg_mines', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION public_geo.pg_drillhole_collars_tiles(
                z integer,
                x integer,
                y integer,
                query_params json DEFAULT '{}'
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            AS $$
            DECLARE
                tile_bbox geometry;
                v         bigint;
            BEGIN
                tile_bbox := ST_TileEnvelope(z, x, y);
                SELECT COALESCE(EXTRACT(EPOCH FROM MAX(updated_at))::bigint, 0)
                INTO v FROM public_geo.jurisdictions;

                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(d.id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        d.jurisdiction_code,
                        d.source_id,
                        d.source_feature_id,
                        d.drillhole_id,
                        d.drillhole_name,
                        d.company,
                        d.project_name,
                        d.drill_type,
                        d.total_length_m,
                        d.has_total_length,
                        d.core_availability,
                        ST_AsMVTGeom(
                            ST_Transform(d.geom, 3857),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM public_geo.v_pg_drillhole_collars_mvt d
                    WHERE ST_Intersects(ST_Transform(d.geom, 3857), tile_bbox)
                    ORDER BY d.id
                )
                SELECT
                    ST_AsMVT(tile, 'pg_drillhole_collars', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION public_geo.pg_rock_samples_tiles(
                z integer,
                x integer,
                y integer,
                query_params json DEFAULT '{}'
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            AS $$
            DECLARE
                tile_bbox geometry;
                v         bigint;
            BEGIN
                tile_bbox := ST_TileEnvelope(z, x, y);
                SELECT COALESCE(EXTRACT(EPOCH FROM MAX(updated_at))::bigint, 0)
                INTO v FROM public_geo.jurisdictions;

                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(r.id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        r.jurisdiction_code,
                        r.source_id,
                        r.source_feature_id,
                        r.station,
                        r.sample_number,
                        r.geologist,
                        r.geographic_area,
                        r.report_number,
                        r.nts_250k,
                        ST_AsMVTGeom(
                            ST_Transform(r.geom, 3857),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM public_geo.v_pg_rock_samples_mvt r
                    WHERE ST_Intersects(ST_Transform(r.geom, 3857), tile_bbox)
                    ORDER BY r.id
                )
                SELECT
                    ST_AsMVT(tile, 'pg_rock_samples', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION public_geo.pg_assessment_surveys_tiles(
                z integer,
                x integer,
                y integer,
                query_params json DEFAULT '{}'
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            AS $$
            DECLARE
                tile_bbox geometry;
                v         bigint;
                simp_tol  double precision;
            BEGIN
                tile_bbox := ST_TileEnvelope(z, x, y);
                SELECT COALESCE(EXTRACT(EPOCH FROM MAX(updated_at))::bigint, 0)
                INTO v FROM public_geo.jurisdictions;

                simp_tol := CASE
                    WHEN z < 8  THEN 100.0
                    WHEN z < 12 THEN 25.0
                    ELSE             5.0
                END;

                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(a.id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        a.jurisdiction_code,
                        a.source_id,
                        a.source_feature_id,
                        a.survey_type,
                        ST_AsMVTGeom(
                            ST_SimplifyPreserveTopology(
                                ST_Transform(a.geom, 3857), simp_tol
                            ),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM public_geo.v_pg_assessment_surveys_mvt a
                    WHERE ST_Intersects(ST_Transform(a.geom, 3857), tile_bbox)
                    ORDER BY a.id
                )
                SELECT
                    ST_AsMVT(tile, 'pg_assessment_surveys', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION public_geo.pg_resource_potential_tiles(
                z integer,
                x integer,
                y integer,
                query_params json DEFAULT '{}'
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            AS $$
            DECLARE
                tile_bbox geometry;
                v         bigint;
                simp_tol  double precision;
            BEGIN
                tile_bbox := ST_TileEnvelope(z, x, y);
                SELECT COALESCE(EXTRACT(EPOCH FROM MAX(updated_at))::bigint, 0)
                INTO v FROM public_geo.jurisdictions;

                simp_tol := CASE
                    WHEN z < 8  THEN 100.0
                    WHEN z < 12 THEN 25.0
                    ELSE             5.0
                END;

                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(r.id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        r.jurisdiction_code,
                        r.source_id,
                        r.commodity,
                        r.commodity_grouping,
                        r.potential_rank,
                        r.has_potential_rank,
                        r.methodology_ref,
                        ST_AsMVTGeom(
                            ST_SimplifyPreserveTopology(
                                ST_Transform(r.geom, 3857), simp_tol
                            ),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM public_geo.v_pg_resource_potential_mvt r
                    WHERE ST_Intersects(ST_Transform(r.geom, 3857), tile_bbox)
                    ORDER BY r.id
                )
                SELECT
                    ST_AsMVT(tile, 'pg_resource_potential', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION public_geo.pg_mineral_dispositions_tiles(
                z integer,
                x integer,
                y integer,
                query_params json DEFAULT '{}'
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            AS $$
            DECLARE
                tile_bbox geometry;
                v         bigint;
                simp_tol  double precision;
            BEGIN
                tile_bbox := ST_TileEnvelope(z, x, y);
                SELECT COALESCE(EXTRACT(EPOCH FROM MAX(updated_at))::bigint, 0)
                INTO v FROM public_geo.jurisdictions;

                simp_tol := CASE
                    WHEN z < 8  THEN 100.0
                    WHEN z < 12 THEN 25.0
                    ELSE             5.0
                END;

                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(d.id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        d.jurisdiction_code,
                        d.source_id,
                        d.source_feature_id,
                        d.disposition_number,
                        d.disposition_type,
                        d.status,
                        d.holder_name,
                        d.issue_date,
                        d.expiry_date,
                        d.area_ha,
                        d.commodity_codes,
                        d.geographic_area,
                        d.source_url,
                        ST_AsMVTGeom(
                            ST_SimplifyPreserveTopology(
                                ST_Transform(d.geom, 3857), simp_tol
                            ),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM public_geo.v_pg_mineral_dispositions_mvt d
                    WHERE ST_Intersects(ST_Transform(d.geom, 3857), tile_bbox)
                    ORDER BY d.id
                )
                SELECT
                    ST_AsMVT(tile, 'pg_mineral_dispositions', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION public_geo.pg_bedrock_geology_tiles(
                z integer,
                x integer,
                y integer,
                query_params json DEFAULT '{}'
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            AS $$
            DECLARE
                tile_bbox geometry;
                v         bigint;
                simp_tol  double precision;
            BEGIN
                tile_bbox := ST_TileEnvelope(z, x, y);
                SELECT COALESCE(EXTRACT(EPOCH FROM MAX(updated_at))::bigint, 0)
                INTO v FROM public_geo.jurisdictions;

                simp_tol := CASE
                    WHEN z < 8  THEN 100.0
                    WHEN z < 12 THEN 25.0
                    ELSE             5.0
                END;

                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(b.id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        b.jurisdiction_code,
                        b.source_id,
                        b.source_feature_id,
                        b.unit_code,
                        b.unit_name,
                        b.eon,
                        b.era,
                        b.period,
                        b.group_name,
                        b.formation,
                        b.member,
                        b.structural_domain,
                        b.lithology,
                        b.scale,
                        ST_AsMVTGeom(
                            ST_SimplifyPreserveTopology(
                                ST_Transform(b.geom, 3857), simp_tol
                            ),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM public_geo.v_pg_bedrock_geology_mvt b
                    WHERE ST_Intersects(ST_Transform(b.geom, 3857), tile_bbox)
                    ORDER BY b.id
                )
                SELECT
                    ST_AsMVT(tile, 'pg_bedrock_geology', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);
    }

    public function down(): void
    {
        // No meaningful rollback — reverting ORDER BY would require restoring
        // the previous function bodies from migrations 130000/140000/140002.
        // The smdi_id bug fix is a correctness fix; reverting it would break
        // pg_mineral_occurrences_tiles at runtime. No-op down() is intentional.
    }
};
