<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Module 8 Chunks 8.1 + 8.2 — §05d tile-freshness spine
 *
 * Creates workspace-scoped MVT tile functions in the `silver` schema and
 * function wrappers for the `public_geo` schema, implementing the
 * §05d addendum signature: RETURNS TABLE (mvt bytea, etag_hash text).
 *
 * ── STOP REPORT — MISSING SILVER SOURCE TABLES ──────────────────────────────
 *
 * The following 4 of 7 silver functions CANNOT be implemented because their
 * source tables do not exist in the silver schema. Kyle must create these
 * tables (or map to existing substitutes) before these functions can land.
 * See the blocked function stubs at the bottom of the `up()` method.
 *
 *   1. pg_boundaries_by_project
 *      Required: silver.project_boundaries
 *      Actual: table does not exist. silver.spatial_features (generic geometry,
 *      no boundary_name/boundary_type columns) is a candidate but requires SME
 *      confirmation that feature_type='boundary' rows carry the needed fields.
 *
 *   2. pg_formations_by_project
 *      Required: silver.geological_formations
 *      Actual: table does not exist. No silver table carries formation_code,
 *      formation_name, age_period. Public Geoscience has pg_bedrock_geology
 *      but that is workspace-global, not project-scoped.
 *
 *   3. pg_historic_workings_by_project
 *      Required: silver.historic_workings
 *      Actual: table does not exist. No silver table carries working_type,
 *      working_name, operational_period.
 *
 *   4. pg_geochem_by_project
 *      Required: silver.geochemistry with a geometry column, project_id,
 *      sample_id, sample_type, assay_element_codes
 *      Actual: silver.geochemistry has no geometry column and no project_id
 *      directly (linked via collar_id FK). It stores major-element oxides
 *      (SiO2/Al2O3/etc.), NOT multi-element assay arrays. The schema does not
 *      match the §05d spec for this layer. Requires schema redesign + SME input
 *      on what constitutes a "geochem point" for mapping purposes.
 *
 * ── IMPLEMENTED FUNCTIONS (3 of 7) ─────────────────────────────────────────
 *
 *   silver.pg_collars_by_project   — point, source: silver.collars (EPSG:32613)
 *   silver.pg_drill_traces_by_project — linestring, source: silver.drill_traces (EPSG:4326)
 *   silver.pg_seismic_by_project   — polygon bbox, source: silver.seismic_surveys (EPSG:4326)
 *
 * ── PUBLIC GEOSCIENCE FUNCTION WRAPPERS (8 of 8) ────────────────────────────
 *
 *   All 8 existing PGEO MVT views get function-source wrappers with the
 *   §05d (mvt bytea, etag_hash text) signature. The views remain for the
 *   existing tables: Martin sources, giving both code paths active.
 *
 *   ETag freshness surrogate for PGEO: EXTRACT(EPOCH FROM MAX(updated_at))
 *   cast to bigint, drawn from public_geo.jurisdictions. Rationale:
 *   last_refreshed_at is NULL for all current rows (feeds have run but the
 *   column is not yet populated by the ingestion pipeline); updated_at is
 *   reliably stamped on every upsert. Once the ingestion pipeline begins
 *   writing last_refreshed_at, Chunk 8.4 proxy work can switch to that column.
 *
 * ── GIST INDEX AUDIT ────────────────────────────────────────────────────────
 *
 *   silver.collars        → idx_collars_geom GIST exists
 *   silver.drill_traces   → idx_drill_traces_geom GIST exists
 *   silver.seismic_surveys → idx_seismic_surveys_bbox GIST exists
 *   No new GIST indexes required for the 3 implemented silver functions.
 *
 * ── ROLE CREATION ───────────────────────────────────────────────────────────
 *
 *   martin_readonly role does not exist yet; created here. Chunk 8.3 will
 *   configure the role's schema-level SELECT grants on all silver + public_geo
 *   tables. This migration grants EXECUTE on the new functions only.
 */
return new class extends Migration
{
    public function up(): void
    {
        // ── martin_readonly role ─────────────────────────────────────────────
        // Created unconditionally; IF NOT EXISTS prevents errors on re-run.
        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'martin_readonly') THEN
                    CREATE ROLE martin_readonly NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE;
                END IF;
            END
            $$;
        SQL);

        // ── GIST index audit stubs ────────────────────────────────────────────
        // All three source tables already have GIST indexes (confirmed by
        // inspection before this migration was authored). These are idempotent
        // safeguards in case the index was ever dropped and recreated without a name.
        DB::statement('CREATE INDEX IF NOT EXISTS idx_collars_geom ON silver.collars USING gist (geom);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_drill_traces_geom ON silver.drill_traces USING gist (geom);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_seismic_surveys_bbox ON silver.seismic_surveys USING gist (bbox);');

        // ════════════════════════════════════════════════════════════════════
        // SILVER FUNCTION 1 — pg_collars_by_project
        // Source: silver.collars (geometry: Point, EPSG:32613 → transformed to 3857)
        // PK:     collar_id (uuid)
        // Properties: project_id, hole_id, collar_azimuth, collar_dip,
        //             total_depth_m, feature_id
        // Simplification: NONE (point layer)
        // ════════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_collars_by_project(
                z integer,
                x integer,
                y integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            AS $$
            DECLARE
                project_id uuid;
                v          bigint;
                tile_bbox  geometry;
            BEGIN
                -- Step 1: extract and validate project_id
                project_id := (query_params->>'project_id')::uuid;
                IF project_id IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                -- Step 2: get data_version; return empty if project row missing
                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = pg_collars_by_project.project_id;

                IF NOT FOUND THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                -- Step 3: compute tile bbox in EPSG:3857
                tile_bbox := ST_TileEnvelope(z, x, y);

                -- Step 4-7: build tile, compute etag
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
                    WHERE c.project_id = pg_collars_by_project.project_id
                      AND ST_Intersects(
                            ST_Transform(c.geom, 3857),
                            tile_bbox
                        )
                )
                SELECT
                    ST_AsMVT(tile, 'collars', 4096, 'geom') AS mvt,
                    md5(
                        v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || project_id::text
                    ) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_collars_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature: RETURNS TABLE(mvt bytea, etag_hash text). Source: silver.collars (EPSG:32613). Transforms to 3857 for tile envelope intersection. ETag = md5(data_version|z|x|y|project_id). Module 8 Chunk 8.1/8.2.'");

        DB::statement('GRANT EXECUTE ON FUNCTION silver.pg_collars_by_project(integer, integer, integer, json) TO martin_readonly;');

        // ════════════════════════════════════════════════════════════════════
        // SILVER FUNCTION 2 — pg_drill_traces_by_project
        // Source: silver.drill_traces (geometry: LineStringZ, EPSG:4326 → 3857)
        // PK:     trace_id (uuid)
        // Properties: project_id, hole_id (via collar join), total_depth_m
        //             (via collar join), feature_id
        // Simplification: ST_SimplifyPreserveTopology — zoom-aware (linestring)
        // Note: hole_id and total_depth_m require a join to silver.collars
        //       because drill_traces does not carry those columns directly.
        // ════════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_drill_traces_by_project(
                z integer,
                x integer,
                y integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            AS $$
            DECLARE
                project_id  uuid;
                v           bigint;
                tile_bbox   geometry;
                simp_tol    double precision;
            BEGIN
                -- Step 1
                project_id := (query_params->>'project_id')::uuid;
                IF project_id IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                -- Step 2
                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = pg_drill_traces_by_project.project_id;

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
                    WHERE dt.project_id = pg_drill_traces_by_project.project_id
                      AND ST_Intersects(
                            ST_Transform(dt.geom, 3857),
                            tile_bbox
                        )
                )
                SELECT
                    ST_AsMVT(tile, 'drill_traces', 4096, 'geom') AS mvt,
                    md5(
                        v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || project_id::text
                    ) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_drill_traces_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.drill_traces JOIN silver.collars (EPSG:4326→3857). Zoom-aware simplification on linestring geometry. Module 8 Chunk 8.1/8.2.'");

        DB::statement('GRANT EXECUTE ON FUNCTION silver.pg_drill_traces_by_project(integer, integer, integer, json) TO martin_readonly;');

        // ════════════════════════════════════════════════════════════════════
        // SILVER FUNCTION 3 — pg_seismic_by_project
        // Source: silver.seismic_surveys (geometry: bbox Polygon, EPSG:4326 → 3857)
        // PK:     survey_id (uuid)
        // Properties: project_id, survey_name, survey_year (derived from created_at),
        //             survey_type, line_count (num_traces proxy), feature_id
        // Simplification: zoom-aware (polygon)
        // Note: silver.seismic_surveys has no `survey_year` column; we derive
        //       EXTRACT(YEAR FROM created_at)::int. No `line_count` column —
        //       we expose `num_traces` aliased as line_count (closest proxy;
        //       SME should confirm or add a dedicated column). Pre-approved V1 item.
        // ════════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_seismic_by_project(
                z integer,
                x integer,
                y integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            AS $$
            DECLARE
                project_id  uuid;
                v           bigint;
                tile_bbox   geometry;
                simp_tol    double precision;
            BEGIN
                -- Step 1
                project_id := (query_params->>'project_id')::uuid;
                IF project_id IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                -- Step 2
                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = pg_seismic_by_project.project_id;

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
                        s.survey_name                              AS survey_name,
                        EXTRACT(YEAR FROM s.created_at)::int       AS survey_year,
                        s.survey_type                              AS survey_type,
                        s.num_traces                               AS line_count,
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
                    WHERE s.project_id = pg_seismic_by_project.project_id
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
                        || '|' || project_id::text
                    ) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_seismic_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.seismic_surveys.bbox (EPSG:4326→3857). survey_year derived from created_at; line_count aliased from num_traces — SME to confirm. Pre-approved V1 item (2026-04-22). Module 8 Chunk 8.1/8.2.'");

        DB::statement('GRANT EXECUTE ON FUNCTION silver.pg_seismic_by_project(integer, integer, integer, json) TO martin_readonly;');

        // ════════════════════════════════════════════════════════════════════
        // BLOCKED SILVER FUNCTIONS — source tables do not exist
        // These are placeholder RAISE EXCEPTION stubs so that if they are
        // accidentally wired into Martin config they produce a clear error
        // rather than a silent 500. Do NOT add them to martin.yaml until
        // the silver tables are created and confirmed by SME.
        // ════════════════════════════════════════════════════════════════════

        // pg_boundaries_by_project — blocked: silver.project_boundaries missing
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_boundaries_by_project(
                z integer,
                x integer,
                y integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            AS $$
            BEGIN
                RAISE EXCEPTION
                    'pg_boundaries_by_project: silver.project_boundaries table does not exist. '
                    'Module 8 Chunk 8.2 STOP — create the table and rewrite this function body before '
                    'wiring to Martin. See database/migrations/2026_04_22_130000_create_silver_mvt_functions.php.';
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_boundaries_by_project(integer, integer, integer, json) IS
            'BLOCKED. Source table silver.project_boundaries does not exist. Raises exception on call. Module 8 Chunk 8.2 STOP.'");

        DB::statement('GRANT EXECUTE ON FUNCTION silver.pg_boundaries_by_project(integer, integer, integer, json) TO martin_readonly;');

        // pg_formations_by_project — blocked: silver.geological_formations missing
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_formations_by_project(
                z integer,
                x integer,
                y integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            AS $$
            BEGIN
                RAISE EXCEPTION
                    'pg_formations_by_project: silver.geological_formations table does not exist. '
                    'Module 8 Chunk 8.2 STOP — create the table and rewrite this function body before '
                    'wiring to Martin. See database/migrations/2026_04_22_130000_create_silver_mvt_functions.php.';
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_formations_by_project(integer, integer, integer, json) IS
            'BLOCKED. Source table silver.geological_formations does not exist. Raises exception on call. Module 8 Chunk 8.2 STOP.'");

        DB::statement('GRANT EXECUTE ON FUNCTION silver.pg_formations_by_project(integer, integer, integer, json) TO martin_readonly;');

        // pg_historic_workings_by_project — blocked: silver.historic_workings missing
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_historic_workings_by_project(
                z integer,
                x integer,
                y integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            AS $$
            BEGIN
                RAISE EXCEPTION
                    'pg_historic_workings_by_project: silver.historic_workings table does not exist. '
                    'Module 8 Chunk 8.2 STOP — create the table and rewrite this function body before '
                    'wiring to Martin. See database/migrations/2026_04_22_130000_create_silver_mvt_functions.php.';
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_historic_workings_by_project(integer, integer, integer, json) IS
            'BLOCKED. Source table silver.historic_workings does not exist. Raises exception on call. Module 8 Chunk 8.2 STOP.'");

        DB::statement('GRANT EXECUTE ON FUNCTION silver.pg_historic_workings_by_project(integer, integer, integer, json) TO martin_readonly;');

        // pg_geochem_by_project — blocked: silver.geochemistry has no geometry
        // column, no project_id, no sample_id/sample_type/assay_element_codes
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_geochem_by_project(
                z integer,
                x integer,
                y integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            AS $$
            BEGIN
                RAISE EXCEPTION
                    'pg_geochem_by_project: silver.geochemistry schema mismatch. '
                    'Table has no geometry column, no project_id, no sample_id/sample_type/'
                    'assay_element_codes. Current schema stores major-element oxides (SiO2/Al2O3/...) '
                    'linked via collar_id FK only. Module 8 Chunk 8.2 STOP — '
                    'redesign schema and rewrite this function. '
                    'See database/migrations/2026_04_22_130000_create_silver_mvt_functions.php.';
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_geochem_by_project(integer, integer, integer, json) IS
            'BLOCKED. silver.geochemistry has no geometry column and schema does not match §05d spec. Raises exception on call. Module 8 Chunk 8.2 STOP.'");

        DB::statement('GRANT EXECUTE ON FUNCTION silver.pg_geochem_by_project(integer, integer, integer, json) TO martin_readonly;');

        // ════════════════════════════════════════════════════════════════════
        // PUBLIC GEOSCIENCE FUNCTION WRAPPERS — 8 of 8
        //
        // ETag freshness surrogate: EXTRACT(EPOCH FROM MAX(updated_at))::bigint
        // from public_geo.jurisdictions. Rationale: last_refreshed_at
        // is NULL for all current rows; updated_at is reliably stamped.
        // Each wrapper invokes ST_AsMVT directly over the existing view, then
        // computes etag_hash from the surrogate + tile coordinates.
        //
        // NOTE: The views return geometry in EPSG:4326; Martin's tables: sources
        // handle their own ST_AsMVT wrapping. These function wrappers call
        // ST_AsMVT themselves and return the two-column §05d contract. Martin
        // must use these as function: sources, not table: sources, to get the
        // etag_hash column. The old table: entries remain active for backward
        // compatibility until Chunk 8.4 migrates the proxy.
        // ════════════════════════════════════════════════════════════════════

        // Helper: shared freshness surrogate query is inlined per function.
        // A shared helper function would require an extra round-trip; inlining
        // keeps each function STABLE + PARALLEL SAFE without cross-function deps.

        // PGEO WRAPPER 1 — pg_mines_tiles
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

                -- Freshness surrogate: latest updated_at across all jurisdictions
                -- coerced to epoch seconds (bigint). last_refreshed_at is NULL for
                -- all current rows and is therefore not used here.
                SELECT COALESCE(
                    EXTRACT(EPOCH FROM MAX(updated_at))::bigint, 0
                ) INTO v
                FROM public_geo.jurisdictions;

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
                )
                SELECT
                    ST_AsMVT(tile, 'pg_mines', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement("COMMENT ON FUNCTION public_geo.pg_mines_tiles(integer, integer, integer, json) IS
            'Martin function-source wrapper for v_pg_mines_mvt. §05d (mvt, etag_hash) signature. ETag freshness from jurisdictions.updated_at epoch. Module 8 Chunk 8.1/8.2.'");
        DB::statement('GRANT EXECUTE ON FUNCTION public_geo.pg_mines_tiles(integer, integer, integer, json) TO martin_readonly;');

        // PGEO WRAPPER 2 — pg_mineral_occurrences_tiles
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
                        o.smdi_id,
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
                )
                SELECT
                    ST_AsMVT(tile, 'pg_mineral_occurrences', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement("COMMENT ON FUNCTION public_geo.pg_mineral_occurrences_tiles(integer, integer, integer, json) IS
            'Martin function-source wrapper for v_pg_mineral_occurrences_mvt. §05d signature. Module 8 Chunk 8.1/8.2.'");
        DB::statement('GRANT EXECUTE ON FUNCTION public_geo.pg_mineral_occurrences_tiles(integer, integer, integer, json) TO martin_readonly;');

        // PGEO WRAPPER 3 — pg_drillhole_collars_tiles
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
                )
                SELECT
                    ST_AsMVT(tile, 'pg_drillhole_collars', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement("COMMENT ON FUNCTION public_geo.pg_drillhole_collars_tiles(integer, integer, integer, json) IS
            'Martin function-source wrapper for v_pg_drillhole_collars_mvt. §05d signature. Module 8 Chunk 8.1/8.2.'");
        DB::statement('GRANT EXECUTE ON FUNCTION public_geo.pg_drillhole_collars_tiles(integer, integer, integer, json) TO martin_readonly;');

        // PGEO WRAPPER 4 — pg_rock_samples_tiles
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
                )
                SELECT
                    ST_AsMVT(tile, 'pg_rock_samples', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement("COMMENT ON FUNCTION public_geo.pg_rock_samples_tiles(integer, integer, integer, json) IS
            'Martin function-source wrapper for v_pg_rock_samples_mvt. §05d signature. Module 8 Chunk 8.1/8.2.'");
        DB::statement('GRANT EXECUTE ON FUNCTION public_geo.pg_rock_samples_tiles(integer, integer, integer, json) TO martin_readonly;');

        // PGEO WRAPPER 5 — pg_assessment_surveys_tiles
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
                                ST_Transform(a.geom, 3857),
                                simp_tol
                            ),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM public_geo.v_pg_assessment_surveys_mvt a
                    WHERE ST_Intersects(ST_Transform(a.geom, 3857), tile_bbox)
                )
                SELECT
                    ST_AsMVT(tile, 'pg_assessment_surveys', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement("COMMENT ON FUNCTION public_geo.pg_assessment_surveys_tiles(integer, integer, integer, json) IS
            'Martin function-source wrapper for v_pg_assessment_surveys_mvt. §05d signature. Multipolygon — zoom-aware simplification. Module 8 Chunk 8.1/8.2.'");
        DB::statement('GRANT EXECUTE ON FUNCTION public_geo.pg_assessment_surveys_tiles(integer, integer, integer, json) TO martin_readonly;');

        // PGEO WRAPPER 6 — pg_resource_potential_tiles
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
                                ST_Transform(r.geom, 3857),
                                simp_tol
                            ),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM public_geo.v_pg_resource_potential_mvt r
                    WHERE ST_Intersects(ST_Transform(r.geom, 3857), tile_bbox)
                )
                SELECT
                    ST_AsMVT(tile, 'pg_resource_potential', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement("COMMENT ON FUNCTION public_geo.pg_resource_potential_tiles(integer, integer, integer, json) IS
            'Martin function-source wrapper for v_pg_resource_potential_mvt. §05d signature. Multipolygon — zoom-aware simplification. Module 8 Chunk 8.1/8.2.'");
        DB::statement('GRANT EXECUTE ON FUNCTION public_geo.pg_resource_potential_tiles(integer, integer, integer, json) TO martin_readonly;');

        // PGEO WRAPPER 7 — pg_mineral_dispositions_tiles
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
                                ST_Transform(d.geom, 3857),
                                simp_tol
                            ),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM public_geo.v_pg_mineral_dispositions_mvt d
                    WHERE ST_Intersects(ST_Transform(d.geom, 3857), tile_bbox)
                )
                SELECT
                    ST_AsMVT(tile, 'pg_mineral_dispositions', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement("COMMENT ON FUNCTION public_geo.pg_mineral_dispositions_tiles(integer, integer, integer, json) IS
            'Martin function-source wrapper for v_pg_mineral_dispositions_mvt. §05d signature. Multipolygon — zoom-aware simplification. Module 8 Chunk 8.1/8.2.'");
        DB::statement('GRANT EXECUTE ON FUNCTION public_geo.pg_mineral_dispositions_tiles(integer, integer, integer, json) TO martin_readonly;');

        // PGEO WRAPPER 8 — pg_bedrock_geology_tiles
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
                                ST_Transform(b.geom, 3857),
                                simp_tol
                            ),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM public_geo.v_pg_bedrock_geology_mvt b
                    WHERE ST_Intersects(ST_Transform(b.geom, 3857), tile_bbox)
                )
                SELECT
                    ST_AsMVT(tile, 'pg_bedrock_geology', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement("COMMENT ON FUNCTION public_geo.pg_bedrock_geology_tiles(integer, integer, integer, json) IS
            'Martin function-source wrapper for v_pg_bedrock_geology_mvt. §05d signature. Multipolygon — zoom-aware simplification. Module 8 Chunk 8.1/8.2.'");
        DB::statement('GRANT EXECUTE ON FUNCTION public_geo.pg_bedrock_geology_tiles(integer, integer, integer, json) TO martin_readonly;');
    }

    public function down(): void
    {
        // Silver functions
        DB::statement('DROP FUNCTION IF EXISTS silver.pg_collars_by_project(integer, integer, integer, json)');
        DB::statement('DROP FUNCTION IF EXISTS silver.pg_drill_traces_by_project(integer, integer, integer, json)');
        DB::statement('DROP FUNCTION IF EXISTS silver.pg_boundaries_by_project(integer, integer, integer, json)');
        DB::statement('DROP FUNCTION IF EXISTS silver.pg_formations_by_project(integer, integer, integer, json)');
        DB::statement('DROP FUNCTION IF EXISTS silver.pg_historic_workings_by_project(integer, integer, integer, json)');
        DB::statement('DROP FUNCTION IF EXISTS silver.pg_seismic_by_project(integer, integer, integer, json)');
        DB::statement('DROP FUNCTION IF EXISTS silver.pg_geochem_by_project(integer, integer, integer, json)');

        // PGEO function wrappers
        DB::statement('DROP FUNCTION IF EXISTS public_geo.pg_mines_tiles(integer, integer, integer, json)');
        DB::statement('DROP FUNCTION IF EXISTS public_geo.pg_mineral_occurrences_tiles(integer, integer, integer, json)');
        DB::statement('DROP FUNCTION IF EXISTS public_geo.pg_drillhole_collars_tiles(integer, integer, integer, json)');
        DB::statement('DROP FUNCTION IF EXISTS public_geo.pg_rock_samples_tiles(integer, integer, integer, json)');
        DB::statement('DROP FUNCTION IF EXISTS public_geo.pg_assessment_surveys_tiles(integer, integer, integer, json)');
        DB::statement('DROP FUNCTION IF EXISTS public_geo.pg_resource_potential_tiles(integer, integer, integer, json)');
        DB::statement('DROP FUNCTION IF EXISTS public_geo.pg_mineral_dispositions_tiles(integer, integer, integer, json)');
        DB::statement('DROP FUNCTION IF EXISTS public_geo.pg_bedrock_geology_tiles(integer, integer, integer, json)');

        // Role: intentionally not dropped — it may have grants from other migrations
    }
};
