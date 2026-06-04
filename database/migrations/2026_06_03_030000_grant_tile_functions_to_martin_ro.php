<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * GRANT EXECUTE on Martin's tile functions to `martin_ro`.
 *
 * Audit item E rationale (2026-06-03)
 * ------------------------------------
 * Martin previously connected as `georag_app` (full read+write on the
 * silver schema via georag_write role membership). A compromise of the
 * Martin process — PostGIS injection through a tile function, SSRF, etc.
 * — would have been a compromise of the entire silver dataset.
 *
 * The architectural fix: switch Martin's `DATABASE_URL` to `martin_ro`
 * (audit migration E3, lands in docker-compose.yml). For that to work,
 * `martin_ro` needs:
 *
 *   1. SELECT on silver tables — already inherited via `georag_read`
 *      membership granted in `zz-grant-app-role-memberships.sql`.
 *   2. EXECUTE on every `pg_*_by_project` / `pg_*_tiles` function
 *      Martin reads — THIS migration.
 *   3. USAGE on the relevant schemas — granted here.
 *
 * Without these grants, Martin returns `permission denied for function`
 * on every tile request after the connection-string swap.
 *
 * Source of truth for the function list: `docker/martin/martin.yaml`.
 * If a new tile function is added to martin.yaml, mirror it here OR
 * watch the per-source 500 in Martin logs.
 *
 * Idempotent: every GRANT is a no-op when re-issued.
 */
return new class extends Migration
{
    /** Schemas Martin reads from. USAGE required. */
    private const SCHEMAS = ['silver', 'public_geo', 'public'];

    /**
     * Tile-rendering functions Martin invokes. Schema-qualified
     * "schema.function_name" — argument signatures resolved at runtime
     * via GRANT EXECUTE ON FUNCTION schema.fn (no args list = all
     * overloads, which is what we want).
     *
     * Source: docker/martin/martin.yaml `functions:` block.
     */
    private const FUNCTIONS = [
        'silver.pg_collars_by_project',
        'silver.pg_drill_traces_by_project',
        'silver.pg_seismic_by_project',
        'silver.pg_boundaries_by_project',
        'silver.pg_formations_by_project',
        'silver.pg_historic_workings_by_project',
        'silver.pg_cross_section_lines_by_project',
        'silver.pg_geochem_by_project',
        'silver.significant_intersections_by_project',
        'silver.density_choropleth_h3',
        'public_geo.pg_mines_tiles',
        'public_geo.pg_mineral_occurrences_tiles',
        'public_geo.pg_drillhole_collars_tiles',
        'public_geo.pg_rock_samples_tiles',
        'public_geo.pg_assessment_surveys_tiles',
        'public_geo.pg_resource_potential_tiles',
        'public_geo.pg_mineral_dispositions_tiles',
        'public_geo.pg_bedrock_geology_tiles',
    ];

    /**
     * Direct table reads Martin makes (not function-wrapped). Needs
     * SELECT — already covered by georag_read membership on silver +
     * public, but listed here for the public table sources that aren't
     * in silver.
     */
    private const TABLES = [
        'public.smdi_deposits',
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // Skip cleanly if martin_ro hasn't been created yet (fresh
        // dev without the postgres-init pass). The companion init
        // script will create it; this migration's grants land on the
        // next run.
        $roleExists = (bool) DB::scalar(
            "SELECT 1 FROM pg_roles WHERE rolname = 'martin_ro'",
        );
        if (! $roleExists) {
            return;
        }

        // 1) Schema USAGE
        foreach (self::SCHEMAS as $schema) {
            DB::statement("GRANT USAGE ON SCHEMA {$schema} TO martin_ro");
        }

        // 2) Function EXECUTE — guard each with IF EXISTS in pg_proc
        // so a renamed/dropped function doesn't block the migration.
        // Iterate every overload (`ON FUNCTION schema.fn` without an
        // arg list is ambiguous when overloads exist; loop through
        // pg_proc to enumerate signatures and grant each explicitly).
        foreach (self::FUNCTIONS as $qualified) {
            [$schema, $fn] = explode('.', $qualified, 2);
            $rows = DB::select(
                'SELECT p.oid::regprocedure::text AS signature '
                .' FROM pg_proc p '
                .' JOIN pg_namespace n ON n.oid = p.pronamespace '
                .' WHERE n.nspname = ? AND p.proname = ?',
                [$schema, $fn],
            );
            foreach ($rows as $row) {
                DB::statement("GRANT EXECUTE ON FUNCTION {$row->signature} TO martin_ro");
            }
            // Log when a documented function is missing — operators
            // need to know martin.yaml drifted from the schema.
            if ($rows === []) {
                error_log(
                    "WARN: tile function {$qualified} not present in pg_proc — "
                    .'martin.yaml references a function that no longer exists. '
                    .'Update martin.yaml or restore the function.',
                );
            }
        }

        // 3) Direct table SELECT (martin.yaml `tables:` block)
        foreach (self::TABLES as $qualified) {
            DB::statement("GRANT SELECT ON {$qualified} TO martin_ro");
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        $roleExists = (bool) DB::scalar(
            "SELECT 1 FROM pg_roles WHERE rolname = 'martin_ro'",
        );
        if (! $roleExists) {
            return;
        }

        // REVOKE in reverse order. Use IF EXISTS where the syntax supports it.
        foreach (self::TABLES as $qualified) {
            DB::statement("REVOKE SELECT ON {$qualified} FROM martin_ro");
        }
        foreach (self::FUNCTIONS as $qualified) {
            [$schema, $fn] = explode('.', $qualified, 2);
            $rows = DB::select(
                'SELECT p.oid::regprocedure::text AS signature '
                .' FROM pg_proc p '
                .' JOIN pg_namespace n ON n.oid = p.pronamespace '
                .' WHERE n.nspname = ? AND p.proname = ?',
                [$schema, $fn],
            );
            foreach ($rows as $row) {
                DB::statement("REVOKE EXECUTE ON FUNCTION {$row->signature} FROM martin_ro");
            }
        }
        foreach (self::SCHEMAS as $schema) {
            DB::statement("REVOKE USAGE ON SCHEMA {$schema} FROM martin_ro");
        }
    }
};
