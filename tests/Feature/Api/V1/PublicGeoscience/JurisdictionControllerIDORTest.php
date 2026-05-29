<?php

namespace Tests\Feature\Api\V1\PublicGeoscience;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

/**
 * Module 10 Chunk 10.3 — JurisdictionController route audit.
 *
 * Module 10 Chunk 10.3 — JurisdictionController has no IDOR surface.
 * Verified routes: GET /api/v1/public-geoscience/jurisdictions
 * The endpoint is workspace-global (public geoscience registry data).
 *
 * Rationale:
 *   GET /api/v1/public-geoscience/jurisdictions returns the jurisdiction
 *   registry — a list of government geological data authorities (e.g. SMDI
 *   for Saskatchewan, MINFILE for BC) grouped by country. This data is:
 *
 *     • Sourced from public_geoscience.jurisdictions and public_geoscience.sources
 *       tables which hold government open-data registry metadata.
 *     • Not project-scoped, not user-owned, and not workspace-scoped.
 *     • Read-only — there is no write path on this controller.
 *     • Identical for every authenticated caller.
 *
 *   There is no URL parameter in the jurisdiction listing that could be swapped
 *   to access another user's data. No IDOR test scenario exists.
 *
 *   The response is cached (5-minute TTL via app's default store). Cache
 *   poisoning is not in scope — there is no user-controlled input.
 *
 *   The auth requirement ensures anonymous enumeration of the government
 *   registry is blocked (ops concern, not IDOR).
 */
class JurisdictionControllerIDORTest extends TestCase
{
    use RefreshDatabase;

    // -------------------------------------------------------------------------
    // Auth gate: unauthenticated request must be denied
    // -------------------------------------------------------------------------

    public function test_unauthenticated_jurisdiction_list_returns_401(): void
    {
        $response = $this->getJson('/api/v1/public-geoscience/jurisdictions');

        $response->assertUnauthorized();
    }

    // -------------------------------------------------------------------------
    // Sanity: any authenticated user can call the jurisdictions endpoint
    // -------------------------------------------------------------------------

    public function test_authenticated_user_can_list_jurisdictions(): void
    {
        $this->skipIfSqlite('public_geoscience schema requires PostgreSQL.');

        $user = User::factory()->create();
        $this->actingAs($user, 'sanctum');

        $response = $this->getJson('/api/v1/public-geoscience/jurisdictions');

        $response->assertOk()
            ->assertJsonStructure(['data', 'generated_at', 'cache_ttl_seconds']);
    }
}
