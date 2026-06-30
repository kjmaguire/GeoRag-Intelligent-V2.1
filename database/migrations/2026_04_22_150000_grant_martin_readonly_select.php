<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Module 8 Chunk 8.3 — martin_readonly SELECT grant audit + remediation
 *
 * Background:
 *   Chunks 8.1/8.2/8.2b created the martin_readonly role and granted EXECUTE
 *   on all 15 MVT functions. Per-table SELECT grants on the underlying source
 *   tables were only partially done (silver.project_boundaries, geological_formations,
 *   historic_workings, geochemistry, projects, workspaces got grants inline in
 *   those migrations). The 8.1 migration granted nothing for silver.collars,
 *   silver.drill_traces, silver.seismic_surveys, or any public_geo table.
 *
 * Root cause of tile errors (seen in Martin logs from 8.2b tests):
 *   All 4 new silver function calls fail with "db error" because martin connects
 *   as martin_readonly (or the georag app user), and the PGEO wrapper functions
 *   access public_geo.jurisdictions for ETag freshness — a table martin
 *   had no SELECT on. Similarly, pg_collars_by_project queries silver.collars
 *   which had no explicit SELECT grant.
 *
 * What this migration does:
 *   1. Grants SELECT on all silver source tables accessed by any of the 7 silver
 *      MVT functions (idempotent — already-granted tables are a no-op in PG).
 *   2. Grants SELECT on all public_geo tables/views accessed by the 8
 *      PGEO wrapper functions (primarily the 8 MVT views + jurisdictions).
 *   3. Installs ALTER DEFAULT PRIVILEGES in both schemas so any table/view
 *      created in the future by migrations is auto-accessible to martin_readonly
 *      without requiring another grant migration.
 *
 * Source table mapping (verified against function bodies in migrations
 * 2026_04_22_130000 and 2026_04_22_140000):
 *
 *   silver.pg_collars_by_project           → silver.collars, silver.projects
 *   silver.pg_drill_traces_by_project      → silver.drill_traces, silver.collars, silver.projects
 *   silver.pg_seismic_by_project           → silver.seismic_surveys, silver.projects
 *   silver.pg_boundaries_by_project        → silver.project_boundaries, silver.projects
 *   silver.pg_formations_by_project        → silver.geological_formations, silver.projects
 *   silver.pg_historic_workings_by_project → silver.historic_workings, silver.projects
 *   silver.pg_geochem_by_project           → silver.geochemistry, silver.projects
 *
 *   public_geo.pg_mines_tiles               → v_pg_mines_mvt, jurisdictions
 *   public_geo.pg_mineral_occurrences_tiles → v_pg_mineral_occurrences_mvt, jurisdictions
 *   public_geo.pg_drillhole_collars_tiles   → v_pg_drillhole_collars_mvt, jurisdictions
 *   public_geo.pg_rock_samples_tiles        → v_pg_rock_samples_mvt, jurisdictions
 *   public_geo.pg_assessment_surveys_tiles  → v_pg_assessment_surveys_mvt, jurisdictions
 *   public_geo.pg_resource_potential_tiles  → v_pg_resource_potential_mvt, jurisdictions
 *   public_geo.pg_mineral_dispositions_tiles → v_pg_mineral_dispositions_mvt, jurisdictions
 *   public_geo.pg_bedrock_geology_tiles     → v_pg_bedrock_geology_mvt, jurisdictions
 *
 * Note: silver.workspaces is already granted (from 8.2b inline grants).
 * silver.projects, project_boundaries, geological_formations, historic_workings,
 * and geochemistry are already granted (from 8.2/8.2b inline grants).
 * All GRANT statements are idempotent — granting an already-granted privilege
 * is a no-op in PostgreSQL, so re-runs are safe.
 */
return new class extends Migration
{
    public function up(): void
    {
        // ════════════════════════════════════════════════════════════════════
        // SCHEMA USAGE — required before any SELECT on tables within a schema
        // martin_readonly needs USAGE on both schemas to resolve table/function
        // names at query time. Without this, even explicit SELECT grants on
        // individual tables fail with "permission denied for schema <name>".
        // ════════════════════════════════════════════════════════════════════
        DB::unprepared('GRANT USAGE ON SCHEMA silver TO martin_readonly;');
        DB::unprepared('GRANT USAGE ON SCHEMA public_geo TO martin_readonly;');

        // ════════════════════════════════════════════════════════════════════
        // SILVER — source tables accessed by the 7 silver MVT functions
        // silver.projects, workspaces, project_boundaries, geological_formations,
        // historic_workings, geochemistry were already granted in prior migrations.
        // This block adds the three missing tables.
        // ════════════════════════════════════════════════════════════════════

        // silver.collars — accessed by pg_collars_by_project (directly) and
        // pg_drill_traces_by_project (JOIN to resolve hole_id + total_depth).
        DB::unprepared('GRANT SELECT ON silver.collars TO martin_readonly;');

        // silver.drill_traces — accessed by pg_drill_traces_by_project.
        DB::unprepared('GRANT SELECT ON silver.drill_traces TO martin_readonly;');

        // silver.seismic_surveys — accessed by pg_seismic_by_project.
        DB::unprepared('GRANT SELECT ON silver.seismic_surveys TO martin_readonly;');

        // ════════════════════════════════════════════════════════════════════
        // PUBLIC_GEOSCIENCE — all 8 MVT views + jurisdictions
        // All 8 PGEO wrapper functions query jurisdictions for the ETag freshness
        // surrogate (EXTRACT(EPOCH FROM MAX(updated_at)) FROM jurisdictions).
        // Each function also queries its corresponding MVT view.
        // ════════════════════════════════════════════════════════════════════

        // Freshness surrogate table — accessed by ALL 8 PGEO wrapper functions.
        DB::unprepared('GRANT SELECT ON public_geo.jurisdictions TO martin_readonly;');

        // MVT views — one per PGEO wrapper function.
        DB::unprepared('GRANT SELECT ON public_geo.v_pg_mines_mvt TO martin_readonly;');
        DB::unprepared('GRANT SELECT ON public_geo.v_pg_mineral_occurrences_mvt TO martin_readonly;');
        DB::unprepared('GRANT SELECT ON public_geo.v_pg_drillhole_collars_mvt TO martin_readonly;');
        DB::unprepared('GRANT SELECT ON public_geo.v_pg_rock_samples_mvt TO martin_readonly;');
        DB::unprepared('GRANT SELECT ON public_geo.v_pg_assessment_surveys_mvt TO martin_readonly;');
        DB::unprepared('GRANT SELECT ON public_geo.v_pg_resource_potential_mvt TO martin_readonly;');
        DB::unprepared('GRANT SELECT ON public_geo.v_pg_mineral_dispositions_mvt TO martin_readonly;');
        DB::unprepared('GRANT SELECT ON public_geo.v_pg_bedrock_geology_mvt TO martin_readonly;');

        // ════════════════════════════════════════════════════════════════════
        // DEFAULT PRIVILEGES — auto-grant SELECT for future tables/views
        // Any table or view created in silver or public_geo by a future
        // migration will automatically get SELECT granted to martin_readonly.
        // This runs as the current connection role (georag app user / superuser).
        // ALTER DEFAULT PRIVILEGES affects objects created by the current role
        // going forward; it does not retroactively grant on existing objects
        // (the explicit grants above cover existing objects).
        // ════════════════════════════════════════════════════════════════════
        DB::unprepared(
            'ALTER DEFAULT PRIVILEGES IN SCHEMA silver GRANT SELECT ON TABLES TO martin_readonly;',
        );
        DB::unprepared(
            'ALTER DEFAULT PRIVILEGES IN SCHEMA public_geo GRANT SELECT ON TABLES TO martin_readonly;',
        );
    }

    public function down(): void
    {
        // Revoke the grants added by this migration only.
        // Grants from prior migrations (8.1/8.2/8.2b) are NOT touched here.
        DB::unprepared('REVOKE USAGE ON SCHEMA silver FROM martin_readonly;');
        DB::unprepared('REVOKE USAGE ON SCHEMA public_geo FROM martin_readonly;');

        DB::unprepared('REVOKE SELECT ON silver.collars FROM martin_readonly;');
        DB::unprepared('REVOKE SELECT ON silver.drill_traces FROM martin_readonly;');
        DB::unprepared('REVOKE SELECT ON silver.seismic_surveys FROM martin_readonly;');

        DB::unprepared('REVOKE SELECT ON public_geo.jurisdictions FROM martin_readonly;');
        DB::unprepared('REVOKE SELECT ON public_geo.v_pg_mines_mvt FROM martin_readonly;');
        DB::unprepared('REVOKE SELECT ON public_geo.v_pg_mineral_occurrences_mvt FROM martin_readonly;');
        DB::unprepared('REVOKE SELECT ON public_geo.v_pg_drillhole_collars_mvt FROM martin_readonly;');
        DB::unprepared('REVOKE SELECT ON public_geo.v_pg_rock_samples_mvt FROM martin_readonly;');
        DB::unprepared('REVOKE SELECT ON public_geo.v_pg_assessment_surveys_mvt FROM martin_readonly;');
        DB::unprepared('REVOKE SELECT ON public_geo.v_pg_resource_potential_mvt FROM martin_readonly;');
        DB::unprepared('REVOKE SELECT ON public_geo.v_pg_mineral_dispositions_mvt FROM martin_readonly;');
        DB::unprepared('REVOKE SELECT ON public_geo.v_pg_bedrock_geology_mvt FROM martin_readonly;');

        // Revoke the default privilege changes.
        DB::unprepared(
            'ALTER DEFAULT PRIVILEGES IN SCHEMA silver REVOKE SELECT ON TABLES FROM martin_readonly;',
        );
        DB::unprepared(
            'ALTER DEFAULT PRIVILEGES IN SCHEMA public_geo REVOKE SELECT ON TABLES FROM martin_readonly;',
        );
    }
};
