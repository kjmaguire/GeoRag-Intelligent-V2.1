<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\Str;
use Inertia\Testing\AssertableInertia;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Smoke test: every project-scoped Foundry route resolves for a project
 * MEMBER, renders the Inertia page component the resolver in
 * resources/js/app.tsx expects, and the routes that were merged into other
 * surfaces still redirect to where they were merged.
 *
 * Split out of FoundryRoutesSmokeTest (2026-10-10). Those cases lived there
 * under `Project::query()->first()` + `markTestSkipped('No projects in DB.')`
 * on a RefreshDatabase test, where no project exists, so all ten of them
 * skipped on every run and the suite reported green. Two of the eight
 * expectations were also stale: /compare and /map are 302 redirects now
 * (routes/web.php), not pages named Foundry/HoleCompare and Foundry/Map, and
 * neither component exists. WorkspaceThreeDPayloadTest documents the same
 * mistake and the fix this follows: seed the fixture.
 *
 * Postgres-only: the pages read silver.* tables with PostGIS / jsonb SQL.
 *   php artisan test -c phpunit.pgsql.xml --filter=FoundryProjectRoutesSmokeTest
 *
 * The org-level routes (no project slug) stay in FoundryRoutesSmokeTest, which
 * also runs on the SQLite suite.
 */
final class FoundryProjectRoutesSmokeTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    protected function setUp(): void
    {
        parent::setUp();

        // OverviewController lists the project's bronze uploads for its ingest
        // tile; keep that off a real object store.
        Storage::fake('s3-bronze');
    }

    /**
     * A member of a real project in a real workspace.
     *
     * @return array{user: User, project: Project}
     */
    private function seedProjectWithMember(string $role = 'viewer'): array
    {
        $user = User::factory()->create();

        $workspaceId = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$workspaceId, 'Routes Smoke Workspace', 'routes-smoke-'.substr($workspaceId, 0, 8)],
        );

        $project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$workspaceId, $project->project_id],
        );
        $user->projects()->syncWithoutDetaching([$project->project_id => ['role' => $role]]);

        return ['user' => $user, 'project' => $project];
    }

    /**
     * @return array<string, array{0: string, 1: string}>
     */
    public static function projectRoutes(): array
    {
        return [
            'overview' => ['', 'Foundry/Overview'],
            'chat' => ['/chat', 'Foundry/Chat'],
            'ingestion-runs' => ['/ingestion-runs', 'Foundry/IngestionRuns'],
            'sources' => ['/sources', 'Foundry/Sources'],
            'workspace' => ['/workspace', 'Foundry/Workspace'],
            'reports' => ['/reports', 'Foundry/Reports'],
        ];
    }

    #[DataProvider('projectRoutes')]
    public function test_project_route_renders_for_a_viewer(string $suffix, string $expectedComponent): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProjectWithMember('viewer');

        $response = $this->actingAs($user)->get('/projects/'.$project->slug.$suffix);

        $response->assertStatus(200);
        $response->assertInertia(fn (AssertableInertia $page) => $page->component($expectedComponent));
    }

    /**
     * /map and /compare were merged into /workspace on 2026-08-19 and are kept
     * as named 302 redirects. /compare lands on the comparison mode rather
     * than MAP, the workspace's default.
     *
     * @return array<string, array{0: string, 1: string}>
     */
    public static function workspaceRedirects(): array
    {
        return [
            'map' => ['/map', '/workspace'],
            'compare' => ['/compare', '/workspace?mode=compare'],
        ];
    }

    #[DataProvider('workspaceRedirects')]
    public function test_merged_route_redirects_into_the_workspace(string $suffix, string $target): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProjectWithMember('viewer');

        $this->actingAs($user)
            ->get('/projects/'.$project->slug.$suffix)
            ->assertStatus(302)
            ->assertRedirect(url('/projects/'.$project->slug.$target));
    }

    /**
     * Routes merged into /reports on 2026-08-18. They are kept as named
     * redirects rather than deleted, so the contract to lock is "still
     * resolves, lands on the merged surface" — not a 404.
     *
     * @return array<string, array{0: string}>
     */
    public static function mergedReportRoutes(): array
    {
        return [
            'ingest-quality' => ['/imports/quality'],
            'corpus' => ['/corpus'],
        ];
    }

    #[DataProvider('mergedReportRoutes')]
    public function test_merged_route_redirects_to_reports(string $suffix): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProjectWithMember('viewer');

        $this->actingAs($user)
            ->get('/projects/'.$project->slug.$suffix)
            ->assertStatus(302)
            ->assertRedirect(url('/projects/'.$project->slug.'/reports'));
    }

    /**
     * The positive cases above only prove something if the same routes refuse
     * a user who is not in the project; otherwise "renders for a viewer" would
     * also pass for a controller that stopped checking membership.
     */
    #[DataProvider('projectRoutes')]
    public function test_project_route_is_not_found_for_a_non_member(string $suffix, string $unusedComponent): void
    {
        ['project' => $project] = $this->seedProjectWithMember('viewer');
        $outsider = User::factory()->create();

        $this->actingAs($outsider)
            ->get('/projects/'.$project->slug.$suffix)
            ->assertNotFound();
    }
}
