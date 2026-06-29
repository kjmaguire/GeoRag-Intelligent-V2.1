<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Tests\TestCase;

/**
 * Pin the workspace.{workspaceId}.activity channel auth shape.
 *
 * Background — 2026-06-02 audit pass 5+ caught the original gate
 * (`return $user->projects()->exists()`) admitting any authenticated
 * user with ANY project to ANY workspace's activity feed. Tenant A
 * could subscribe to tenant B's workspace channel just by knowing the
 * workspace UUID. The fix scopes the access check to
 * `silver.projects.workspace_id` matching the channel parameter.
 *
 * Live channel-auth invocation requires the broadcaster to be wired
 * with a registered driver + a real DB connection — heavy for a
 * regression that only needs to detect the "any-project-anywhere"
 * idiom returning. A string-level assertion against `routes/channels.php`
 * catches the regression without standing up the broadcaster: if a
 * future edit drops the `workspace_id` scoping, this test fires.
 *
 * The companion docs are docs/handover/AUDIT_AND_FIX_REPORT.md
 * (Theme F — Reverb cross-tenant leak).
 */
class WorkspaceActivityChannelAuthTest extends TestCase
{
    private function channelsFile(): string
    {
        $path = base_path('routes/channels.php');
        $this->assertFileExists($path);

        return (string) file_get_contents($path);
    }

    public function test_channel_callback_scopes_to_workspace_id(): void
    {
        $contents = $this->channelsFile();

        $this->assertMatchesRegularExpression(
            '/silver\.projects\.workspace_id/',
            $contents,
            'workspace.{workspaceId}.activity channel auth must scope by '.
            'silver.projects.workspace_id. Without this scoping, any '.
            'authenticated tenant-A user could subscribe to tenant-B activity '.
            'channels — see Theme F in '.
            'docs/handover/AUDIT_AND_FIX_REPORT.md.',
        );
    }

    public function test_channel_callback_rejects_unauthenticated_users(): void
    {
        $contents = $this->channelsFile();

        // The fix introduced an explicit `if ($user === null)` guard.
        // Detecting it by pattern is enough — if the literal disappears
        // someone simplified the callback in a way that re-opens the
        // anonymous-subscribe path.
        $this->assertMatchesRegularExpression(
            '/Broadcast::channel\(\'workspace\.\{workspaceId\}\.activity\'.*?\$user === null/s',
            $contents,
            'workspace.{workspaceId}.activity channel auth must reject '.
            'unauthenticated users with an explicit null check.',
        );
    }

    public function test_channel_callback_validates_workspace_uuid_shape(): void
    {
        $contents = $this->channelsFile();

        // UUID validation is part of the "don't leak existence on
        // malformed input" pattern shared with query.{queryId}.
        $this->assertMatchesRegularExpression(
            '/Broadcast::channel\(\'workspace\.\{workspaceId\}\.activity\'.*?preg_match\(\s*\'.*?\[0-9a-f\]\{8\}/s',
            $contents,
            'workspace.{workspaceId}.activity must validate that the '.
            'subscribed channel name is a well-formed UUID before doing '.
            'any DB lookup.',
        );
    }
}
