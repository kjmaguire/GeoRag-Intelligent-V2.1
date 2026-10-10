<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Inertia\Testing\AssertableInertia;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\TestCase;

/**
 * Smoke test: every org-level Foundry route (no project slug) resolves to a
 * 200 OK Inertia response for an authenticated user, and the Inertia page
 * component name matches what the resolver in resources/js/app.tsx expects.
 *
 * Runs on the SQLite suite too. The project-scoped routes (/projects/{slug}/…)
 * and their merged-route redirects live in FoundryProjectRoutesSmokeTest, which
 * needs a seeded project and therefore Postgres: they used to sit here behind
 * `Project::query()->first()` + markTestSkipped and skipped on every run.
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
}
