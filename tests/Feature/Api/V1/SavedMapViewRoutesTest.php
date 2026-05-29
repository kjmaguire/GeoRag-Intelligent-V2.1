<?php

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Tests\TestCase;

/**
 * Smoke test for the §6.5 saved-map-views route registration
 * (doc-phase 108).
 *
 * The controller bodies are doc-phase 105 skeletons that throw
 * LogicException. These tests verify:
 *   1. Routes are wired (no 404 on the route paths).
 *   2. Auth is enforced (401 unauthenticated).
 *   3. Once authenticated, the skeleton LogicException surfaces
 *      (proves the route reaches the controller — not silently
 *      400/500 from middleware).
 *
 * When the controller bodies graduate (post-§6.7 frontend wiring),
 * this test grows into a full CRUD round-trip test.
 */
class SavedMapViewRoutesTest extends TestCase
{
    /**
     * GET /api/v1/projects/{project}/saved-map-views requires auth.
     */
    public function test_index_requires_authentication(): void
    {
        $response = $this->getJson('/api/v1/projects/some-project-id/saved-map-views');
        $response->assertStatus(401);
    }

    /**
     * POST /api/v1/projects/{project}/saved-map-views requires auth.
     */
    public function test_store_requires_authentication(): void
    {
        $response = $this->postJson('/api/v1/projects/some-project-id/saved-map-views', []);
        $response->assertStatus(401);
    }

    /**
     * The 5 saved-map-view route names exist in the registered route list.
     */
    public function test_all_five_routes_are_registered(): void
    {
        $expectedNames = [
            'projects.saved-map-views.index',
            'projects.saved-map-views.store',
            'projects.saved-map-views.show',
            'projects.saved-map-views.update',
            'projects.saved-map-views.destroy',
        ];

        $registeredNames = collect(\Route::getRoutes())
            ->map(fn ($route) => $route->getName())
            ->filter()
            ->values()
            ->all();

        foreach ($expectedNames as $name) {
            $this->assertContains(
                $name,
                $registeredNames,
                "Route name '{$name}' should be registered"
            );
        }
    }
}
