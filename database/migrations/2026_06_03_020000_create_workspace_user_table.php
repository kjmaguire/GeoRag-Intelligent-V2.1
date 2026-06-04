<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

/**
 * Pivot table linking users to workspaces with a role.
 *
 * Why this exists (2026-06-03 audit, item A)
 * -------------------------------------------
 * The 2026-06-02/03 audit caught ~12 distinct bugs that all shared the
 * same root cause: code was reading `$user->workspace_id` (which doesn't
 * exist as a column) and falling back to a hardcoded default-tenant UUID.
 * That fallback silently mis-routed data into the default workspace for
 * every non-default tenant — broken multi-tenancy by design.
 *
 * The architectural fix is to give users a first-class workspace
 * membership relationship, parallel to the existing `project_user`
 * pivot. With this table in place:
 *
 *   - HandleInertiaRequests shares the workspaces a user belongs to
 *     plus a current_workspace_id once per request.
 *   - Every page / controller stops resolving workspace from "first
 *     project the user happens to own" hacks.
 *   - Agent-code `or "a0000000-..."` fallbacks become a typed
 *     `WorkspaceContext` that fails loud when the JWT didn't carry
 *     a workspace claim.
 *   - Workspace-level operations (admin, billing, audit) don't require
 *     being on every project in the workspace (the project_user table
 *     can't model that cleanly).
 *
 * Roles
 * -----
 *   owner   : full workspace CRUD + member management + billing
 *   admin   : member management + read all project + admin surfaces
 *   member  : query / chat / export within the workspace
 *   viewer  : read-only access (future use)
 *
 * Backfill
 * --------
 * Every existing user who has at least one `project_user` row to a
 * project in workspace X gets a `workspace_user` row for X with role
 * derived from the highest project-level role they hold in that
 * workspace (owner > member > viewer). This preserves current access.
 *
 * The pivot lives in the public schema — same rationale as
 * `project_user`. It's an application-level membership table, not
 * geological domain data.
 */
return new class extends Migration
{
    public function up(): void
    {
        Schema::create('workspace_user', function (Blueprint $table): void {
            $table->id();
            $table->foreignId('user_id')->constrained('users')->cascadeOnDelete();
            $table->foreignUuid('workspace_id');
            $table->string('role', 20)->default('member'); // owner | admin | member | viewer
            $table->timestamps();

            $table->unique(['user_id', 'workspace_id']);
            $table->index('workspace_id');
        });

        // FK to silver.workspaces — silver schema is on a different namespace
        // from the public-schema workspace_user table, so use a raw FK ADD
        // for the cross-schema reference (matches project_user pattern).
        // Skip on sqlite (test DB) since silver.workspaces lives in pgsql only.
        if (DB::connection()->getDriverName() === 'pgsql') {
            DB::statement(
                'ALTER TABLE workspace_user'
                .' ADD CONSTRAINT workspace_user_workspace_id_fkey'
                .' FOREIGN KEY (workspace_id) REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE',
            );
        }

        // CHECK constraint pinning the role allow-list. Matches the
        // documented roles in the docblock above.
        if (DB::connection()->getDriverName() === 'pgsql') {
            DB::statement(
                'ALTER TABLE workspace_user'
                .' ADD CONSTRAINT workspace_user_role_valid'
                ." CHECK (role IN ('owner', 'admin', 'member', 'viewer'))",
            );
        }

        // ── Backfill from existing project membership ─────────────────
        // For every (user_id, workspace_id) pair reachable via
        // project_user → silver.projects.workspace_id, insert a
        // workspace_user row. Role is the highest role the user holds
        // in any project in that workspace. (owner > member > viewer).
        //
        // Skipped on sqlite — silver.projects only exists on pgsql.
        if (DB::connection()->getDriverName() === 'pgsql') {
            DB::statement(<<<'SQL'
                INSERT INTO workspace_user (user_id, workspace_id, role, created_at, updated_at)
                SELECT
                    pu.user_id,
                    p.workspace_id,
                    -- Highest role wins. ROW_NUMBER on a deterministic
                    -- ordering inside the workspace.
                    CASE
                        WHEN bool_or(pu.role = 'owner')  THEN 'owner'
                        WHEN bool_or(pu.role = 'member') THEN 'member'
                        ELSE 'viewer'
                    END,
                    NOW(),
                    NOW()
                FROM project_user pu
                JOIN silver.projects p ON p.project_id = pu.project_id
                WHERE p.workspace_id IS NOT NULL
                GROUP BY pu.user_id, p.workspace_id
                ON CONFLICT (user_id, workspace_id) DO NOTHING
            SQL);
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'pgsql') {
            DB::statement('ALTER TABLE workspace_user DROP CONSTRAINT IF EXISTS workspace_user_role_valid');
            DB::statement('ALTER TABLE workspace_user DROP CONSTRAINT IF EXISTS workspace_user_workspace_id_fkey');
        }
        Schema::dropIfExists('workspace_user');
    }
};
