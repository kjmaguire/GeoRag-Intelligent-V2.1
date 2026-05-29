<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Drillhole schema — Martin tile function for significant intersections.
 *
 * Drilldown map layer: every significant intersection appears as a
 * proportional circle at the COLLAR location, colour-mapped by
 * element and sized by weighted_avg grade. Geologists open the map,
 * pick a project, and instantly see where the high grades are.
 *
 * Function signature follows the §05d Martin convention:
 *   (z, x, y, params json) → bytea
 *
 * Params supported:
 *   workspace_id  — mandatory tenant filter
 *   element       — 'Au' (default), 'Cu', 'Ag', 'U', etc.
 *   min_grade     — minimum weighted_avg to include (default 0)
 *   cutoff_grade  — match a specific cutoff bucket (optional)
 *   project_id    — narrow to one project (optional)
 *
 * The function lives in schema `silver` so the existing Martin
 * config block picks it up.
 *
 * Granted to martin_reader (read-only Martin db role).
 *
 * SQLite (test DB) — gated on Postgres.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.significant_intersections_by_project(
              z integer, x integer, y integer, params json DEFAULT '{}'::json
            )
            RETURNS bytea
            LANGUAGE plpgsql STABLE PARALLEL SAFE
            SECURITY DEFINER
            SET search_path = pg_catalog, public
            AS $func$
            DECLARE
              tile bytea;
              ws_id      uuid    := NULLIF(params->>'workspace_id', '')::uuid;
              elem       text    := COALESCE(params->>'element', 'Au');
              min_grade  numeric := COALESCE((params->>'min_grade')::numeric, 0);
              cutoff     numeric := NULLIF(params->>'cutoff_grade', '')::numeric;
              proj_id    uuid    := NULLIF(params->>'project_id', '')::uuid;
            BEGIN
              IF ws_id IS NULL THEN
                -- Without a workspace, Martin would happily emit every
                -- tenant's intercepts on one tile. Refuse the request
                -- by returning an empty MVT.
                RETURN '\x'::bytea;
              END IF;

              SELECT ST_AsMVT(q, 'significant_intersections', 4096, 'geom')
              INTO tile
              FROM (
                SELECT
                  si.id,
                  c.hole_id,
                  si.element,
                  si.cutoff_grade,
                  si.weighted_avg,
                  si.unit,
                  si.from_depth,
                  si.to_depth,
                  si.downhole_length,
                  si.zone_name,
                  c.total_depth,
                  ST_AsMVTGeom(
                    ST_Transform(c.geom_4326, 3857),
                    ST_TileEnvelope(z, x, y),
                    4096, 64, true
                  ) AS geom
                FROM gold.significant_intersections si
                JOIN silver.collars c ON c.collar_id = si.collar_id
                WHERE si.workspace_id = ws_id
                  AND si.element = elem
                  AND si.weighted_avg >= min_grade
                  AND (cutoff IS NULL OR si.cutoff_grade = cutoff)
                  AND (proj_id IS NULL OR c.project_id = proj_id)
                  AND c.geom_4326 IS NOT NULL
                  AND ST_Transform(c.geom_4326, 3857) && ST_TileEnvelope(z, x, y)
              ) q;

              RETURN COALESCE(tile, '\x'::bytea);
            END
            $func$
        SQL);

        // Comment for the Martin /catalog UI.
        DB::statement(<<<'SQL'
            COMMENT ON FUNCTION silver.significant_intersections_by_project(
              integer, integer, integer, json
            ) IS
              'Martin tile source. Proportional-circle layer of significant assay intersections at collar location. Params: workspace_id (required), element=''Au'', min_grade=0, cutoff_grade=NULL, project_id=NULL.'
        SQL);

        // Grant to the read-only Martin role. If the role doesn't
        // exist yet (older deployments), this is a no-op via the
        // DO block — we don't want to fail the migration on a
        // role-config issue.
        DB::statement(<<<'SQL'
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'martin_reader') THEN
                GRANT EXECUTE ON FUNCTION silver.significant_intersections_by_project(
                  integer, integer, integer, json
                ) TO martin_reader;
              END IF;
            END
            $$
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('DROP FUNCTION IF EXISTS silver.significant_intersections_by_project(integer, integer, integer, json)');
    }
};
