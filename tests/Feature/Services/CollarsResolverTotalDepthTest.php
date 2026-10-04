<?php

declare(strict_types=1);

namespace Tests\Feature\Services;

use App\Services\Citations\Resolvers\CollarsResolver;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * silver.collars.total_depth is optional since 2026-09-29 (§04e,
 * SME-approved). A citation card must say so rather than print "0.0 m TD" —
 * a depth nobody measured, shown as if it were one.
 */
final class CollarsResolverTotalDepthTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    public function test_a_collar_without_total_depth_is_described_honestly(): void
    {
        $workspaceId = (string) Str::uuid();
        $projectId = (string) Str::uuid();
        $collarId = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug) VALUES (?::uuid, ?, ?)',
            [$workspaceId, 'resolver-ws', 'resolver-'.substr($workspaceId, 0, 8)],
        );
        DB::statement(
            'INSERT INTO silver.projects (project_id, project_name, slug, workspace_id) VALUES (?::uuid, ?, ?, ?::uuid)',
            [$projectId, 'resolver-project', 'resolver-'.substr($projectId, 0, 8), $workspaceId],
        );
        DB::statement(
            "INSERT INTO silver.collars (collar_id, hole_id, project_id, workspace_id, easting, northing,
                    total_depth, hole_type, status)
             VALUES (?::uuid, 'NO-EOH-1', ?::uuid, ?::uuid, 1, 2, NULL, 'RC', 'active')",
            [$collarId, $projectId, $workspaceId],
        );

        $payload = (new CollarsResolver)
            ->resolve("silver.collars:count=1:first={$collarId}", $workspaceId, [$projectId])
            ->getData(true);

        $this->assertStringContainsString('TD not recorded', $payload['text']);
        $this->assertStringNotContainsString('0.0 m TD', $payload['text']);
    }
}
