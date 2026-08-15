<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * DB dimension push-to-9.5 sweep (2026-08-15) — test-DB parity gap.
 *
 * `workspace.workspace_roles` and `workspace.workspace_memberships` are
 * created ONLY by `database/raw/phase0/10-layer-a-workspace-foundation.sql`
 * — there is no Laravel migration equivalent anywhere in
 * `database/migrations/`, so a migrate-only Postgres (CI's pgsql test DB,
 * a fresh dev clone) never gets either table.
 *
 * Confirmed load-bearing, not dead schema:
 *   - `tests/Feature/Tenancy/WorkspaceRlsCoverageTest.php:272` explicitly
 *     expects `['workspace', 'workspace_memberships', 'tenant_isolation']`
 *     in its RLS-coverage table list.
 *   - `2026_05_25_185013_normalize_layered_workspace_isolation_policies_
 *     phase2.php` and `2026_08_14_030000_close_rls_admin_escape_hatch_
 *     verified_subset.php` both target
 *     `workspace.workspace_memberships` — both guarded by a
 *     `tableExists()` check, so on a migrate-only DB they silently
 *     no-op instead of erroring, meaning the table (and the RLS policy
 *     that was supposed to land on it) is just quietly absent rather than
 *     causing a visible migration failure. Same shape of bug the 2026-05-25
 *     `enable_rls_on_phase0_workspace_tables_reconciliation` migration
 *     already fixed once for 14 *other* raw-SQL-only tables (audit_ledger,
 *     cross_section_panels, drillhole_intervals_visual, geochemistry,
 *     etc.) — workspace_roles/workspace_memberships were missed by that
 *     sweep.
 *
 * This migration transcribes `10-layer-a-workspace-foundation.sql`
 * verbatim (CREATE TABLE IF NOT EXISTS + the three system-role seed rows,
 * ON CONFLICT DO NOTHING) so the pgsql test DB — and any other
 * migrate-only environment — ends up with the exact same schema and seed
 * data production already has. Safe no-op on any environment where the
 * phase0 raw SQL already ran.
 *
 * RLS is deliberately NOT added here — `2026_05_25_185013` and
 * `2026_08_14_030000` already own applying RLS to
 * workspace.workspace_memberships once the table exists, and re-running
 * this migration ordered before them (both are dated earlier than
 * 2026-08-15) means their `tableExists()` guard will still skip on a
 * fresh DB unless migration order is respected. Since Laravel always
 * applies migrations in filename/timestamp order and this file is dated
 * *after* both, a fresh `migrate` run creates the table too late for
 * either earlier migration to pick it up — flagged as a follow-up rather
 * than silently duct-taped here with an out-of-order RLS policy that
 * could drift from those migrations' canonical shape. See the accompanying
 * DB-audit report for the explicit call-out.
 *
 * Skipped on sqlite (workspace.* + jsonb + uuid FKs to silver.workspaces
 * don't exist there).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // The `workspace` schema itself has the same gap one level up —
        // it's created only by docker/postgresql/init/10-phase0-extensions-
        // and-schemas.sql (a docker-entrypoint-initdb.d script, never run
        // by `php artisan migrate`), and no `provision_workspace_schema_
        // for_test_db` migration exists (unlike audit/workflow/usage,
        // which each got one — 2026_05_14_140000/140100/140400). Without
        // this, CREATE TABLE workspace.workspace_roles below would fail
        // with "schema workspace does not exist" on a truly fresh
        // migrate-only DB.
        DB::statement('CREATE SCHEMA IF NOT EXISTS workspace');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS workspace.workspace_roles (
                id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id    uuid        NULL REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                name            text        NOT NULL,
                description     text        NULL,
                permissions     jsonb       NOT NULL DEFAULT '[]'::jsonb,
                is_system       boolean     NOT NULL DEFAULT false,
                created_at      timestamptz NOT NULL DEFAULT now(),
                updated_at      timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT workspace_roles_name_per_scope UNIQUE (workspace_id, name)
            )
        SQL);

        DB::statement("COMMENT ON TABLE  workspace.workspace_roles IS 'RBAC role definitions; workspace_id NULL = global system role.'");
        DB::statement("COMMENT ON COLUMN workspace.workspace_roles.permissions IS 'Array of permission strings, e.g. [\"audit.read\",\"report.signoff\"].'");
        DB::statement("COMMENT ON COLUMN workspace.workspace_roles.is_system IS 'TRUE for platform-curated roles that customers cannot delete.'");

        DB::statement(
            'CREATE INDEX IF NOT EXISTS workspace_roles_workspace_id_idx
             ON workspace.workspace_roles (workspace_id) WHERE workspace_id IS NOT NULL',
        );

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS workspace.workspace_memberships (
                id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id         bigint      NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
                workspace_id    uuid        NOT NULL REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                role_id         uuid        NOT NULL REFERENCES workspace.workspace_roles(id) ON DELETE RESTRICT,
                invited_by      bigint      NULL REFERENCES public.users(id) ON DELETE SET NULL,
                invited_at      timestamptz NULL,
                accepted_at     timestamptz NULL,
                created_at      timestamptz NOT NULL DEFAULT now(),
                updated_at      timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT workspace_memberships_user_workspace UNIQUE (user_id, workspace_id)
            )
        SQL);

        DB::statement("COMMENT ON TABLE  workspace.workspace_memberships IS 'User-to-workspace membership with role binding.'");
        DB::statement("COMMENT ON COLUMN workspace.workspace_memberships.invited_at IS 'NULL for self-created (workspace owner) memberships; set when invitation sent.'");
        DB::statement("COMMENT ON COLUMN workspace.workspace_memberships.accepted_at IS 'NULL until invitee accepts (or self-creation, set to created_at).'");

        DB::statement(
            'CREATE INDEX IF NOT EXISTS workspace_memberships_workspace_id_idx
             ON workspace.workspace_memberships (workspace_id)',
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS workspace_memberships_user_id_idx
             ON workspace.workspace_memberships (user_id)',
        );

        DB::statement(<<<'SQL'
            INSERT INTO workspace.workspace_roles (workspace_id, name, description, permissions, is_system)
            VALUES
                (NULL, 'workspace_admin',  'Full administrative control of a workspace.',
                    '["workspace.manage","membership.manage","agent.config","audit.read","report.signoff"]'::jsonb, true),
                (NULL, 'workspace_member', 'Standard member: read+write within workspace, no admin.',
                    '["workspace.read","workspace.write","report.read","audit.read.own"]'::jsonb, true),
                (NULL, 'workspace_viewer', 'Read-only member: dashboards and reports.',
                    '["workspace.read","report.read"]'::jsonb, true)
            ON CONFLICT (workspace_id, name) DO NOTHING
        SQL);

        DB::statement('GRANT USAGE ON SCHEMA workspace TO georag_app');
        DB::statement('GRANT SELECT, INSERT, UPDATE ON workspace.workspace_roles TO georag_app');
        DB::statement('GRANT SELECT, INSERT, UPDATE, DELETE ON workspace.workspace_memberships TO georag_app');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP TABLE IF EXISTS workspace.workspace_memberships CASCADE');
        DB::statement('DROP TABLE IF EXISTS workspace.workspace_roles CASCADE');
    }
};
