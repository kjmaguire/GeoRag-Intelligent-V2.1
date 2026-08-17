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
            'ingest-quality' => ['/imports/quality', 'Foundry/IngestQuality'],
            'ingestion-runs' => ['/ingestion-runs', 'Foundry/IngestionRuns'],
            'sources' => ['/sources', 'Foundry/Sources'],
            'corpus' => ['/corpus', 'Foundry/Corpus'],
            'compare' => ['/compare', 'Foundry/HoleCompare'],
            'workspace' => ['/workspace', 'Foundry/Workspace'],
            'reports' => ['/reports', 'Foundry/Report'],
            'map' => ['/map', 'Foundry/Map'],
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
}
