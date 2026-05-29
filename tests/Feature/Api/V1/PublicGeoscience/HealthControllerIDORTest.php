<?php

namespace Tests\Feature\Api\V1\PublicGeoscience;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

/**
 * Module 10 Chunk 10.3 — HealthController route audit.
 *
 * Module 10 Chunk 10.3 — HealthController has no IDOR surface.
 * Verified routes: GET /api/v1/public-geoscience/health
 * The endpoint is workspace-global / admin-ops polling surface.
 *
 * Rationale:
 *   GET /api/v1/public-geoscience/health returns an aggregated health
 *   payload covering PostGIS row counts, staleness, Martin reachability,
 *   and Qdrant collection counts. All data is derived from workspace-global
 *   public government open-data tables (public_geoscience.*) and
 *   infrastructure status — not from any per-user or per-project resource.
 *
 *   An authenticated User A calling this endpoint sees the exact same payload
 *   as User B. There is no resource identifier in the URL that could be swapped
 *   to access another user's data. No IDOR test scenario exists.
 *
 *   The only meaningful security concern is that unauthenticated callers should
 *   not be able to probe internal infrastructure status. That auth gate is
 *   verified here.
 */
class HealthControllerIDORTest extends TestCase
{
    use RefreshDatabase;

    // -------------------------------------------------------------------------
    // Auth gate: unauthenticated health check must be denied
    // -------------------------------------------------------------------------

    public function test_unauthenticated_health_check_returns_401(): void
    {
        $response = $this->getJson('/api/v1/public-geoscience/health');

        $response->assertUnauthorized();
    }

    // -------------------------------------------------------------------------
    // Sanity: any authenticated user can call health (it is a global status check)
    // The response shape is verified; exact status (200/503) depends on stack.
    // -------------------------------------------------------------------------

    public function test_authenticated_user_can_call_health_endpoint(): void
    {
        $this->skipIfSqlite('public_geoscience schema requires PostgreSQL.');

        $user = User::factory()->create();
        $this->actingAs($user, 'sanctum');

        $response = $this->getJson('/api/v1/public-geoscience/health');

        // Either 200 (all green/warn) or 503 (critical) is valid — the endpoint
        // is not under test for correctness of health logic here, only accessibility.
        $this->assertContains($response->status(), [200, 503]);
        $response->assertJsonStructure(['overall', 'checked_at', 'checks']);
    }
}
