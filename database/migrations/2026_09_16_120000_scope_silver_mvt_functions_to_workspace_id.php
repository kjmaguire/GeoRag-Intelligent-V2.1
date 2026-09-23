<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Close the Martin tile-function tenant-isolation gap.
 *
 * ## The bug
 *
 * Martin (`docker/martin/martin.yaml`) connects to Postgres directly, as its
 * own role (`martin_readonly` in production per `deploy/aws/bootstrap.sql`;
 * `georag_app` in compose per `docker-compose.yml`) — never through a
 * Laravel/FastAPI session. The 9 workspace-scoped `silver.pg_*_by_project`
 * MVT function-sources are `SECURITY INVOKER` and never call `set_config`, so
 * `current_setting('app.workspace_id', true)` is unset for every Martin
 * request. Whether that fails safe (a fail-closed policy like
 * `silver.collars`'s `collars_workspace_isolation` in
 * database/raw/phase0/96-rls-tenant-isolation-block1.sql, which returns ZERO
 * rows when the GUC is unset — "blank map, no cross-tenant leak") or fails
 * open (the OR-unset-admits-all shape most other tables carry —
 * `<table>_workspace_isolation` installed by
 * 2026_05_25_180924_replace_broken_guc_rls_policies_with_canonical.php and
 * 2026_08_19_020000_reconcile_rls_on_undeclared_workspace_tables.php —
 * "every tenant's rows, to everyone") depends on which RLS policy this
 * cluster happens to carry for that table, which in turn depends on whether
 * the out-of-band `database/raw/phase0/9[5-9]-rls-*.sql` layer was ever
 * applied here (ops/runbooks/raw-sql-layer.md: "CD has never applied" it in
 * some environments).
 *
 * Worse: on a database built from the Laravel migration chain ALONE (no
 * database/raw/ layer — e.g. a fresh CI database, or any environment that
 * trusted the now-corrected CLAUDE.md claim that migrations are
 * self-sufficient), `silver.collars` carries only the policy installed by
 * 2026_04_13_200000_production_hardening_final.php:
 *
 *     CREATE POLICY collars_owner_access ON silver.collars FOR ALL USING (true)
 *
 * `USING (true)` is not fail-closed OR fail-open — it is no filter at all.
 * On that database silver.pg_collars_by_project is not "blank map for
 * everyone"; it is a full cross-tenant leak of every workspace's collars
 * through Martin, project_id-scoped only. The bug's actual severity is
 * therefore deployment-dependent and the explicit predicate this migration
 * adds is not merely defense-in-depth on top of RLS — for that class of
 * deployment it is the *only* thing that filters by tenant at all.
 *
 * ## The fix, per function
 *
 *  1. Require `workspace_id` inside `query_params` (the existing Martin URL
 *     param mechanism already used by `significant_intersections_by_project`,
 *     migration 2026_05_20_061000, and `density_choropleth_h3`,
 *     database/raw/phase0/105-section6-density-mvt-function.sql). Missing or
 *     malformed `workspace_id` RAISEs rather than silently returning an
 *     empty tile — the caller must be told it is filtering nothing, not shown
 *     a blank map indistinguishable from "no data in this tile".
 *  2. `PERFORM set_config('app.workspace_id', v_wsid::text, true)` — arms
 *     whichever RLS policy this cluster's silver tables carry, using the
 *     canonical GUC name confirmed by database/raw/phase0/95-99-rls-*.sql,
 *     2026_05_30_010000_enable_rls_silver_drill_traces.php, and
 *     database/tests/pgtap/11_rls_workspace_isolation.sql ("ALL current
 *     policies read app.workspace_id"). `is_local = true` is correct here:
 *     the whole function body runs as one statement in one implicit
 *     transaction, so the setting is visible to every subsequent query in
 *     this call and is discarded at the end of it — no cross-request leakage
 *     on a connection-reusing pool.
 *  3. An explicit `AND <table>.workspace_id = v_wsid` predicate in the main
 *     query, and `AND p.workspace_id = v_wsid` in the `silver.projects`
 *     data_version lookup (so a project_id from a different workspace than
 *     the claimed workspace_id resolves to "project not found", not to
 *     another tenant's data_version or rows). This is the layer that holds
 *     regardless of which RLS policy shape — or absence of the raw/ SQL
 *     layer — this cluster has.
 *
 * `pg_cross_section_lines_by_project` (2026_05_22_020000) gets an incidental
 * second fix while its body is rewritten here: it still declared its local
 * variable `project_id` and referenced it as
 * `pg_cross_section_lines_by_project.project_id`, the exact pattern
 * 2026_04_22_140001/140002 document as broken ("PostgreSQL disambiguates
 * `<fn>.project_id` as a table.column reference ... missing FROM-clause entry
 * for table"). That variable-shadow hotfix was applied to every other
 * function created before 2026_05_22 but never to this one, created after.
 * Renamed to `v_pid` to match every sibling function and actually work.
 *
 * `significant_intersections_by_project` (2026_05_20_061000) is NOT touched
 * here — it already requires `workspace_id` from `query_params` and filters
 * with an explicit `si.workspace_id = ws_id` predicate. It is SECURITY
 * DEFINER and never calls set_config, so it does not depend on the RLS
 * layer at all; it is the existing correct model this migration follows.
 *
 * `pg_boundaries_by_project` / `pg_formations_by_project` /
 * `pg_historic_workings_by_project` / `pg_geochem_by_project` are NOT the
 * RAISE EXCEPTION stubs docker/martin/martin.yaml's comment block (lines
 * 49-55) still describes as "BLOCKED... NOT wired here because their source
 * tables do not exist" — that comment is stale. Migration
 * 2026_04_22_140000_create_silver_boundary_formation_working_geochem.php
 * created the four backing tables and 2026_04_22_140001 replaced the stub
 * bodies with real queries the same day; docker/martin/martin.yaml already
 * lists all four as active `functions:` entries (lines 83-126). This
 * migration updates the stale comment block alongside the workspace-scoping
 * fix.
 *
 * ## Volatility
 *
 * Every one of these functions was `STABLE PARALLEL SAFE`. `set_config()` is
 * volatile (it mutates session state) and not parallel-safe. Declaring a
 * function STABLE when its body has a session-visible side effect is exactly
 * the kind of thing the query planner is trusted not to need to worry about,
 * so all nine functions are changed to plain `VOLATILE` (the default — the
 * keyword is omitted) with no `PARALLEL SAFE`/`PARALLEL RESTRICTED` claim.
 * Martin calls these as the sole function-source of a single-row tile
 * request; there is no plan-quality loss in practice.
 *
 * Idempotent: CREATE OR REPLACE FUNCTION. SQLite (test DB) — gated on
 * Postgres, matching every other MVT-function migration in this chain.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // ════════════════════════════════════════════════════════════════
        // 1 — silver.pg_collars_by_project
        // Source: silver.collars (EPSG:32613 → 3857). Carries the
        // uncertainty-rings properties added by 2026_05_24_130000.
        // ════════════════════════════════════════════════════════════════
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
                      AND c.workspace_id = v_wsid
                      AND ST_Intersects(ST_Transform(c.geom, 3857), tile_bbox)
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
            'Martin function-source. §05d signature. Source: silver.collars (EPSG:32613→3857). Requires workspace_id in query_params (raises if missing/invalid); arms app.workspace_id via set_config and filters explicitly on workspace_id as defense-in-depth alongside RLS. Publishes spatial_uncertainty_m + crs_confidence + georef_method + _lat. ORDER BY collar_id for deterministic ST_AsMVT. Tenant-isolation fix 2026-09-16.'");

        // ════════════════════════════════════════════════════════════════
        // 2 — silver.pg_drill_traces_by_project
        // Source: silver.drill_traces JOIN silver.collars (EPSG:4326 → 3857)
        // ════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
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

        DB::statement("COMMENT ON FUNCTION silver.pg_drill_traces_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.drill_traces JOIN silver.collars. Requires workspace_id in query_params (raises if missing/invalid); arms app.workspace_id via set_config and filters explicitly on dt.workspace_id. Tenant-isolation fix 2026-09-16.'");

        // ════════════════════════════════════════════════════════════════
        // 3 — silver.pg_seismic_by_project
        // Source: silver.seismic_surveys.bbox (EPSG:4326 → 3857)
        // ════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
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

        DB::statement("COMMENT ON FUNCTION silver.pg_seismic_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.seismic_surveys.bbox (EPSG:4326→3857). Requires workspace_id in query_params (raises if missing/invalid); arms app.workspace_id via set_config and filters explicitly on s.workspace_id. Tenant-isolation fix 2026-09-16.'");

        // ════════════════════════════════════════════════════════════════
        // 4 — silver.pg_boundaries_by_project
        // Source: silver.project_boundaries (MultiPolygon, EPSG:4326 → 3857)
        // ════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
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

        DB::statement("COMMENT ON FUNCTION silver.pg_boundaries_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.project_boundaries (MultiPolygon, EPSG:4326→3857). Requires workspace_id in query_params (raises if missing/invalid); arms app.workspace_id via set_config and filters explicitly on b.workspace_id. Tenant-isolation fix 2026-09-16.'");

        // ════════════════════════════════════════════════════════════════
        // 5 — silver.pg_formations_by_project
        // Source: silver.geological_formations (MultiPolygon, EPSG:4326 → 3857)
        // ════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
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

        DB::statement("COMMENT ON FUNCTION silver.pg_formations_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.geological_formations (MultiPolygon, EPSG:4326→3857). Requires workspace_id in query_params (raises if missing/invalid); arms app.workspace_id via set_config and filters explicitly on f.workspace_id. Tenant-isolation fix 2026-09-16.'");

        // ════════════════════════════════════════════════════════════════
        // 6 — silver.pg_historic_workings_by_project
        // Source: silver.historic_workings (Point, EPSG:4326 → 3857)
        // ════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
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

        DB::statement("COMMENT ON FUNCTION silver.pg_historic_workings_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.historic_workings (Point, EPSG:4326→3857). Requires workspace_id in query_params (raises if missing/invalid); arms app.workspace_id via set_config and filters explicitly on hw.workspace_id. Tenant-isolation fix 2026-09-16.'");

        // ════════════════════════════════════════════════════════════════
        // 7 — silver.pg_geochem_by_project
        // Source: silver.geochemistry (Point, EPSG:4326)
        // ════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
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

        DB::statement("COMMENT ON FUNCTION silver.pg_geochem_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.geochemistry (Point EPSG:4326). Requires workspace_id in query_params (raises if missing/invalid); arms app.workspace_id via set_config and filters explicitly on gc.workspace_id. Tenant-isolation fix 2026-09-16.'");

        // ════════════════════════════════════════════════════════════════
        // 8 — silver.pg_spatial_features_by_project
        // Source: silver.spatial_features (mixed geometry, EPSG:4326)
        // Three MVT layers (imported_points/lines/polygons) in one tile.
        // ════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_spatial_features_by_project(
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
                tile_bbox geometry;   -- EPSG:3857, the MVT output frame
                bbox_4326 geometry;   -- same envelope in 4326, for the indexed prefilter
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
                    RAISE EXCEPTION 'silver.pg_spatial_features_by_project: workspace_id in query_params is not a valid UUID';
                END;
                IF v_wsid IS NULL THEN
                    RAISE EXCEPTION 'silver.pg_spatial_features_by_project: workspace_id is required in query_params (tenant-scoped tile source)';
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
                      AND sf.workspace_id = v_wsid
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
                        || '|' || v_pid::text || '|' || v_wsid::text
                    ) AS etag_hash;
            END;
            $$;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_spatial_features_by_project(integer, integer, integer, json) IS
            'MVT function-source. §05d signature. Source: silver.spatial_features (mixed geometry, EPSG:4326). Emits imported_points / imported_lines / imported_polygons in one tile. Requires workspace_id in query_params (raises if missing/invalid); arms app.workspace_id via set_config and filters explicitly on sf.workspace_id. Tenant-isolation fix 2026-09-16.'");

        // ════════════════════════════════════════════════════════════════
        // 9 — silver.pg_cross_section_lines_by_project
        // Source: gold.cross_section_panels.section_line_geom (LineString, 4326)
        //
        // Also fixes a latent, pre-existing bug: the 2026_05_22_020000
        // version declared its local variable `project_id` and referenced it
        // as `pg_cross_section_lines_by_project.project_id` in the
        // silver.projects lookup — the exact variable-shadow pattern
        // 2026_04_22_140001/140002 document as raising "missing FROM-clause
        // entry for table pg_cross_section_lines_by_project" at runtime.
        // That hotfix reached every function created before 2026-05-22 but
        // not this one, created after. Renamed to v_pid here to match every
        // sibling function.
        // ════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
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

        DB::statement("COMMENT ON FUNCTION silver.pg_cross_section_lines_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: gold.cross_section_panels.section_line_geom (EPSG:4326). Requires workspace_id in query_params (raises if missing/invalid); arms app.workspace_id via set_config and filters explicitly on p.workspace_id. Also fixes a pre-existing variable-shadow bug (v_pid rename) that raised missing-FROM-clause errors on every call. Tenant-isolation fix 2026-09-16.'");
    }

    public function down(): void
    {
        // Deliberately no-op. Reverting to the pre-fix bodies would either
        // restore "blank map for everyone" (fail-closed RLS environments) or
        // restore a full cross-tenant leak (fail-open / no-RLS-layer
        // environments, e.g. silver.collars on a database built from the
        // Laravel migration chain alone — see the up() docblock). Neither is
        // a state to roll back into. A genuine revert should be a new,
        // reviewed migration, not `migrate:rollback`.
    }
};
