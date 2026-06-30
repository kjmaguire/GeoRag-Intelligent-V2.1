<?php

namespace Tests\Feature\Api\V1\PublicGeoscience;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

/**
 * Module 10 Chunk 10.3 — IDOR regression tests for EntityReferencesController.
 *
 * Routes under test:
 *   GET /api/v1/public-geoscience/entities/{canonical_type}/{pg_id}/references
 *   GET /api/v1/public-geoscience/documents/{report_id}/references
 *
 * Scoping model: Public Geoscience entities and their cross-corpus links are
 * WORKSPACE-GLOBAL. They represent government-published open data (SK SMDI,
 * BC MINFILE) and are intentionally readable by any authenticated user. There
 * is no per-user or per-project ownership — a "mine" entity in the SMDI corpus
 * is the same record regardless of which project is querying it.
 *
 * The controller:
 *   - Validates canonical_type against a fixed whitelist (returns 404 on unknown type).
 *   - Validates pg_id and report_id as UUIDs (returns 400 on malformed input).
 *   - Returns an empty `documents: []` payload for a valid UUID that has no links.
 *
 * IDOR analysis: there is no cross-tenant isolation to test because PGEO records
 * are shared. The IDOR surface is limited to:
 *   1. Unauthenticated access must be denied (401).
 *   2. An invalid canonical_type returns 404 (not 500).
 *   3. A malformed UUID returns 400 (not 500).
 *   4. A valid UUID with no links returns a graceful empty payload (not a data leak).
 *
 * No user-A-vs-user-B cross-tenant test is appropriate here because there are no
 * user-owned records in the public_geoscience schema.
 *
 * Verified routes:
 *   GET /api/v1/public-geoscience/entities/{canonical_type}/{pg_id}/references
 *   GET /api/v1/public-geoscience/documents/{report_id}/references
 * Both are workspace-global (public government data). No per-user IDOR test needed.
 */
class EntityReferencesControllerIDORTest extends TestCase
{
    use RefreshDatabase;

    private User $userA;

    protected function setUp(): void
    {
        parent::setUp();

        $this->userA = User::factory()->create();
    }

    // -------------------------------------------------------------------------
    // Auth gate: unauthenticated requests must be denied
    // -------------------------------------------------------------------------

    public function test_unauthenticated_entity_references_returns_401(): void
    {
        $validUuid = '00000000-0000-0000-0000-000000000001';

        $response = $this->getJson(
            "/api/v1/public-geoscience/entities/mine/{$validUuid}/references",
        );

        $response->assertUnauthorized();
    }

    public function test_unauthenticated_document_references_returns_401(): void
    {
        $validUuid = '00000000-0000-0000-0000-000000000001';

        $response = $this->getJson(
            "/api/v1/public-geoscience/documents/{$validUuid}/references",
        );

        $response->assertUnauthorized();
    }

    // -------------------------------------------------------------------------
    // Invalid canonical_type → 404 (not 500 or data leak)
    // -------------------------------------------------------------------------

    public function test_unknown_canonical_type_returns_404(): void
    {
        $this->actingAs($this->userA, 'sanctum');

        $validUuid = '00000000-0000-0000-0000-000000000001';

        $response = $this->getJson(
            "/api/v1/public-geoscience/entities/nonexistent_type/{$validUuid}/references",
        );

        // Route constraint `where('canonical_type', 'mine|...')` blocks unknown values
        // at the routing layer, returning 404 before the controller even runs.
        $response->assertNotFound();
    }

    // -------------------------------------------------------------------------
    // Malformed pg_id UUID → 400 (controller input validation)
    // -------------------------------------------------------------------------

    public function test_malformed_pg_id_returns_400(): void
    {
        $this->actingAs($this->userA, 'sanctum');

        $response = $this->getJson(
            '/api/v1/public-geoscience/entities/mine/NOT-A-UUID/references',
        );

        $response->assertStatus(400)
            ->assertJsonPath('message', 'Invalid pg_id UUID.');
    }

    public function test_malformed_report_id_returns_400(): void
    {
        $this->actingAs($this->userA, 'sanctum');

        $response = $this->getJson(
            '/api/v1/public-geoscience/documents/NOT-A-UUID/references',
        );

        $response->assertStatus(400)
            ->assertJsonPath('message', 'Invalid report_id UUID.');
    }

    // -------------------------------------------------------------------------
    // Valid UUID with no data → graceful empty payload (no data leak, no 500)
    // -------------------------------------------------------------------------

    public function test_valid_uuid_with_no_links_returns_empty_payload(): void
    {
        $this->skipIfSqlite('public_geoscience schema tables require PostgreSQL.');

        $this->actingAs($this->userA, 'sanctum');

        // A well-formed UUID that won't exist in any test DB.
        $absentUuid = 'ffffffff-ffff-ffff-ffff-ffffffffffff';

        $response = $this->getJson(
            "/api/v1/public-geoscience/entities/mine/{$absentUuid}/references",
        );

        $response->assertOk()
            ->assertJsonPath('total', 0)
            ->assertJsonPath('documents', []);
    }
}
