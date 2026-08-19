<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Inertia\Testing\AssertableInertia;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\TestCase;

/**
 * Smoke test: every Foundry route resolves to a 200 OK Inertia response for
 * an authenticated user, and the Inertia page component name matches what
 * the resolver in resources/js/app.tsx expects.
 *
 * Two test phases:
 *   1. Org-scoped routes (no project slug)
 *   2. Project-scoped routes — only run if a project exists in the DB
 */
final class FoundryRoutesSmokeTest extends TestCase
{
    use RefreshDatabase;

    public static function orgRoutes(): array
    {
        return [
            'projects' => ['/projects', 'Foundry/Projects'],
            'imports' => ['/foundry/imports/wizard', 'Foundry/DataImportWizard'],
            'newproject' => ['/foundry/projects/new', 'Foundry/NewProject'],
            'public-geoscience' => ['/public-geoscience', 'Foundry/PublicGeoscience'],
        ];
    }

    #[DataProvider('orgRoutes')]
    public function test_org_route_renders_for_authenticated_user(string $url, string $expectedComponent): void
    {
        $user = User::factory()->create();

        $response = $this->actingAs($user)->get($url);

        $response->assertStatus(200);
        $response->assertInertia(fn (AssertableInertia $page) => $page->component($expectedComponent));
    }

    public function test_login_page_renders_unauthenticated(): void
    {
        $response = $this->get('/login');

        $response->assertStatus(200);
        $response->assertInertia(fn (AssertableInertia $page) => $page->component('Login'));
    }

    public static function projectRoutes(): array
    {
        return [
            'overview' => ['', 'Foundry/Overview'],
            'chat' => ['/chat', 'Foundry/Chat'],
            'ingestion-runs' => ['/ingestion-runs', 'Foundry/IngestionRuns'],
            'sources' => ['/sources', 'Foundry/Sources'],
            'compare' => ['/compare', 'Foundry/HoleCompare'],
            'workspace' => ['/workspace', 'Foundry/Workspace'],
            'reports' => ['/reports', 'Foundry/Reports'],
            'map' => ['/map', 'Foundry/Map'],
        ];
    }

    /**
     * Routes merged into /reports on 2026-08-18. They are kept as named
     * redirects rather than deleted, so the contract to lock is "still
     * resolves, lands on the merged surface" — not a 404.
     */
    public static function mergedProjectRoutes(): array
    {
        return [
            'ingest-quality' => ['/imports/quality'],
            'corpus' => ['/corpus'],
        ];
    }

    #[DataProvider('projectRoutes')]
    public function test_project_route_renders_for_member(string $suffix, string $expectedComponent): void
    {
        $project = Project::query()->first();

        if (! $project) {
            $this->markTestSkipped('No projects in DB.');
        }

        $user = User::factory()->create();
        $project->users()->syncWithoutDetaching([$user->id => ['role' => 'viewer']]);

        $url = '/projects/'.$project->slug.$suffix;
        $response = $this->actingAs($user)->get($url);

        $response->assertStatus(200);
        $response->assertInertia(fn (AssertableInertia $page) => $page->component($expectedComponent));
    }

    #[DataProvider('mergedProjectRoutes')]
    public function test_merged_route_redirects_to_reports(string $suffix): void
    {
        $project = Project::query()->first();

        if (! $project) {
            $this->markTestSkipped('No projects in DB.');
        }

        $user = User::factory()->create();
        $user->projects()->syncWithoutDetaching([
            $project->project_id => ['role' => 'owner'],
        ]);

        $this->actingAs($user)
            ->get("/projects/{$project->slug}{$suffix}")
            ->assertRedirect("/projects/{$project->slug}/reports");
    }
}
