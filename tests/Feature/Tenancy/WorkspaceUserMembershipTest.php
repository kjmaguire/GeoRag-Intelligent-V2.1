<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Tests\TestCase;

/**
 * Pin the `workspace_user` pivot migration contract.
 *
 * Added with the 2026-06-03 audit item A — user→workspace association.
 * Without this pivot table the User model's `workspaces()` /
 * `hasWorkspaceAccess()` / `defaultWorkspaceId()` methods all fall
 * back to "derive from project_user", which the audit established
 * was the wrong long-term architecture (see AUDIT_AND_FIX_REPORT.md
 * item A rationale).
 *
 * Strategy mirrors WorkspaceActivityChannelAuthTest: assert against
 * the migration FILE contents so the test runs anywhere (no live DB
 * required). The actual DB-shape verification happens in CI when
 * `RefreshDatabase` runs the migration; that's an environmental
 * concern. This test catches the "someone deleted the migration
 * file" regression which is what really matters for the contract.
 */
class WorkspaceUserMembershipTest extends TestCase
{
    private function migrationContents(): string
    {
        $path = base_path('database/migrations/2026_06_03_020000_create_workspace_user_table.php');
        $this->assertFileExists(
            $path,
            'workspace_user migration missing. Item A from the 2026-06-03 '
            .'audit (user→workspace association) depends on this table.',
        );

        return (string) file_get_contents($path);
    }

    public function test_migration_creates_workspace_user_table(): void
    {
        $contents = $this->migrationContents();

        $this->assertMatchesRegularExpression(
            "/Schema::create\\(\\s*'workspace_user'/",
            $contents,
            'Migration must create the workspace_user table.',
        );
    }

    public function test_migration_declares_required_columns(): void
    {
        $contents = $this->migrationContents();

        // user_id FK
        $this->assertMatchesRegularExpression(
            "/foreignId\\(\\s*'user_id'\\s*\\)->constrained\\(\\s*'users'\\s*\\)->cascadeOnDelete/",
            $contents,
            'workspace_user.user_id must be a constrained foreignId to users with CASCADE.',
        );

        // workspace_id as UUID
        $this->assertMatchesRegularExpression(
            "/foreignUuid\\(\\s*'workspace_id'\\s*\\)/",
            $contents,
            'workspace_user.workspace_id must be a foreignUuid.',
        );

        // role with default
        $this->assertMatchesRegularExpression(
            "/string\\(\\s*'role'/",
            $contents,
            'workspace_user.role must be a string column.',
        );
    }

    public function test_migration_pins_unique_constraint(): void
    {
        $contents = $this->migrationContents();

        $this->assertMatchesRegularExpression(
            "/->unique\\(\\s*\\[\\s*'user_id'\\s*,\\s*'workspace_id'\\s*\\]/",
            $contents,
            'workspace_user must have a UNIQUE constraint on (user_id, workspace_id).',
        );
    }

    public function test_migration_pgsql_pins_role_check_constraint(): void
    {
        $contents = $this->migrationContents();

        $this->assertMatchesRegularExpression(
            '/workspace_user_role_valid.*owner.*admin.*member.*viewer/s',
            $contents,
            'Postgres CHECK constraint workspace_user_role_valid must allowlist '
            .'owner | admin | member | viewer. Anything else is a footgun '
            .'(silent role drift like the ollama/backend_used case the audit caught).',
        );
    }

    public function test_migration_pgsql_backfills_from_project_user(): void
    {
        $contents = $this->migrationContents();

        $this->assertMatchesRegularExpression(
            '/INSERT INTO workspace_user.*FROM project_user.*JOIN silver\\.projects/s',
            $contents,
            'Migration must backfill workspace_user from existing project '
            .'memberships so existing users keep access on first deploy.',
        );

        $this->assertStringContainsString(
            'ON CONFLICT (user_id, workspace_id) DO NOTHING',
            $contents,
            'Backfill must be idempotent (ON CONFLICT DO NOTHING) so a retry '
            .'doesnt fail on rerun.',
        );
    }

    public function test_user_model_exposes_workspace_methods(): void
    {
        $userPath = base_path('app/Models/User.php');
        $this->assertFileExists($userPath);
        $src = (string) file_get_contents($userPath);

        foreach ([
            'workspaces' => 'BelongsToMany relationship to silver.workspaces',
            'hasWorkspaceAccess' => 'fail-closed access check',
            'workspaceRole' => 'role lookup helper',
            'defaultWorkspaceId' => 'session-default seed',
        ] as $method => $purpose) {
            $this->assertMatchesRegularExpression(
                "/public function {$method}\\b/",
                $src,
                "User::{$method}() missing — {$purpose}. Required by ".
                'HandleInertiaRequests workspace context share.',
            );
        }
    }

    public function test_handle_inertia_requests_shares_workspace_context(): void
    {
        $middlewarePath = base_path('app/Http/Middleware/HandleInertiaRequests.php');
        $this->assertFileExists($middlewarePath);
        $src = (string) file_get_contents($middlewarePath);

        $this->assertStringContainsString(
            "'current_workspace_id'",
            $src,
            'HandleInertiaRequests must share auth.user.current_workspace_id '
            .'so the React layer stops reading localStorage / hardcoded '
            .'default-tenant UUIDs. (Item A from the 2026-06-03 audit.)',
        );
        $this->assertStringContainsString(
            "'workspaces'",
            $src,
            'HandleInertiaRequests must share auth.user.workspaces[] so a '
            .'future workspace-switcher UI has the data it needs without '
            .'a server round-trip.',
        );
    }
}
