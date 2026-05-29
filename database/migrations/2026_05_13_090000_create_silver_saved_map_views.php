<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create silver.saved_map_views — user-saved MapLibre map states
 * (camera + active layers + filters + AOI geometry).
 *
 * Master-plan §6.5 deliverable (doc-phase 76).
 *
 * Auth model picked for autonomous run: per-user, per-project, workspace-
 * scoped. Tabled for Kyle to confirm at 8am pickup (§6 open question #3).
 *
 * `view_state` is JSONB so the MapLibre camera / layer-pack /
 * filter / draw-layer state can evolve without schema migrations.
 * Pattern matches phase3 silver tables (doc-phase 50) which also use
 * JSONB-heavy payloads for the same reason.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('SET search_path TO silver, public;');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.saved_map_views (
                view_id        UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id   UUID         NOT NULL,
                project_id     UUID         NOT NULL,
                user_id        BIGINT       NOT NULL,
                name           VARCHAR(120) NOT NULL,
                description    TEXT         NULL,
                view_state     JSONB        NOT NULL DEFAULT '{}'::jsonb,
                aoi_geom       geometry(Geometry, 4326) NULL,
                is_shared      BOOLEAN      NOT NULL DEFAULT false,
                created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

                CONSTRAINT saved_map_views_pkey
                    PRIMARY KEY (view_id),

                CONSTRAINT saved_map_views_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE,

                CONSTRAINT saved_map_views_project_id_fkey
                    FOREIGN KEY (project_id)
                    REFERENCES silver.projects (project_id)
                    ON DELETE CASCADE,

                CONSTRAINT saved_map_views_user_id_fkey
                    FOREIGN KEY (user_id)
                    REFERENCES public.users (id)
                    ON DELETE CASCADE,

                CONSTRAINT saved_map_views_name_per_user_project_unique
                    UNIQUE (project_id, user_id, name),

                CONSTRAINT saved_map_views_name_nonblank
                    CHECK (length(trim(name)) > 0)
            );
        SQL);

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_saved_map_views_workspace
             ON silver.saved_map_views (workspace_id);'
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_saved_map_views_project
             ON silver.saved_map_views (project_id);'
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_saved_map_views_user
             ON silver.saved_map_views (user_id);'
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_saved_map_views_aoi_gist
             ON silver.saved_map_views USING GIST (aoi_geom)
             WHERE aoi_geom IS NOT NULL;'
        );

        // RLS — same pattern as other silver tables. Workspace-scoped
        // via app.workspace_id session setting (see §RLS docs).
        DB::statement(
            'ALTER TABLE silver.saved_map_views ENABLE ROW LEVEL SECURITY;'
        );
        // Doc-phase 172 — DROP-first makes the migration re-runnable under
        // `migrate:fresh` which keeps non-public schemas + their policies
        // between RefreshDatabase cycles.
        DB::statement('DROP POLICY IF EXISTS saved_map_views_workspace_isolation ON silver.saved_map_views;');
        DB::statement(<<<'SQL'
            CREATE POLICY saved_map_views_workspace_isolation
                ON silver.saved_map_views
                USING (workspace_id::text = current_setting('app.workspace_id', true))
                WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));
        SQL);
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.saved_map_views;');
    }
};
