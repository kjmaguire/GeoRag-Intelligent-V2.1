<?php

declare(strict_types=1);

namespace Tests\Feature;

use App\Models\User;
use Tests\TestCase;

/**
 * Phase 39 R-P11-B slice 1 — feature coverage for the /search Inertia page.
 *
 * The page is a static skeleton at slice 1: no server-side props, no
 * controller, no DB reads. Assertions cover only the auth contract and
 * Inertia page identifier so the test stays fast and DB-driver agnostic.
 *
 *   - guest → 302 redirect to /login (auth:sanctum gate)
 *   - authenticated → 200 + Inertia component "SearchQuery"
 */
class SearchQueryPageTest extends TestCase
{
    private const ENDPOINT = '/search';

    public function test_guest_is_redirected_to_login(): void
    {
        $response = $this->get(self::ENDPOINT);
        $response->assertRedirect('/login');
    }

    public function test_authenticated_user_sees_search_page(): void
    {
        $user = User::factory()->create();
        $this->actingAs($user, 'sanctum');

        $response = $this->withHeader('X-Inertia', 'true')->get(self::ENDPOINT);
        $response->assertOk();
        $response->assertHeader('X-Inertia', 'true');
        $response->assertJsonPath('component', 'SearchQuery');
    }
}
