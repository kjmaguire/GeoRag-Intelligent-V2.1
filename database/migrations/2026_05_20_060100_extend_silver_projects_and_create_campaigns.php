<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Drillhole schema — Silver tier: project + campaign layer.
 *
 * Two things:
 *
 *   1. Extend silver.projects with the columns the drillhole schema
 *      doc asks for. The existing table has 120 rows and the PK is
 *      `project_id` (not `id` as the doc suggests). We KEEP the
 *      existing PK column name — downstream code, Martin functions,
 *      and the FastAPI orchestrator all use `project_id`.
 *
 *   2. Create silver.campaigns (drilling programs / exploration
 *      phases). FK to silver.projects(project_id).
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

        // ── 1. Extend silver.projects ─────────────────────────────────────
        DB::statement(<<<'SQL'
            ALTER TABLE silver.projects
              ADD COLUMN IF NOT EXISTS project_code   text,
              ADD COLUMN IF NOT EXISTS province_state text,
              ADD COLUMN IF NOT EXISTS country        text DEFAULT 'Canada',
              ADD COLUMN IF NOT EXISTS commodity_arr  text[],
              ADD COLUMN IF NOT EXISTS deposit_type   text,
              ADD COLUMN IF NOT EXISTS geom_boundary  geometry(Polygon, 4326)
        SQL);

        // commodity_arr (vs existing varchar commodity) — the existing
        // column is a single varchar; the schema doc wants text[]. Keep
        // both so any code reading `commodity` still works; new code
        // populates BOTH from the array on write.
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.projects.commodity_arr IS
              'Multi-value commodity list. The legacy `commodity` varchar carries the FIRST entry for backward compat; new writers SHOULD populate this array and let the legacy column track commodity_arr[1].'
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS silver_projects_geom_boundary_idx ON silver.projects USING gist(geom_boundary)');
        DB::statement('CREATE UNIQUE INDEX IF NOT EXISTS silver_projects_workspace_code_idx ON silver.projects (workspace_id, project_code) WHERE project_code IS NOT NULL');

        // ── 2. Create silver.campaigns ────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.campaigns (
                id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id    uuid NOT NULL,
                project_id      uuid NOT NULL REFERENCES silver.projects(project_id),
                campaign_code   text NOT NULL,
                campaign_name   text,
                drill_type      text,
                start_date      date,
                end_date        date,
                total_holes     integer,
                total_metres    numeric,
                contractor      text,
                geologist       text,
                notes           text,
                created_at      timestamptz NOT NULL DEFAULT now(),
                UNIQUE (workspace_id, campaign_code)
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS silver_campaigns_workspace_project_idx ON silver.campaigns (workspace_id, project_id)');

        DB::statement("COMMENT ON TABLE silver.campaigns IS 'Drilling programs / exploration phases. One row per logical campaign; many holes ↔ one campaign via silver.collars.campaign_id.'");
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS silver.campaigns');
        DB::statement('DROP INDEX IF EXISTS silver.silver_projects_workspace_code_idx');
        DB::statement('DROP INDEX IF EXISTS silver.silver_projects_geom_boundary_idx');
        DB::statement(<<<'SQL'
            ALTER TABLE silver.projects
              DROP COLUMN IF EXISTS geom_boundary,
              DROP COLUMN IF EXISTS deposit_type,
              DROP COLUMN IF EXISTS commodity_arr,
              DROP COLUMN IF EXISTS country,
              DROP COLUMN IF EXISTS province_state,
              DROP COLUMN IF EXISTS project_code
        SQL);
    }
};
