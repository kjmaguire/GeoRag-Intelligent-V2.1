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
 * Scoping model: the public_geo.* entities themselves (pg_mine,
 * pg_mineral_occurrence, etc.) ARE workspace-global — government-published
 * open data (SK SMDI, BC MINFILE) readable by any authenticated user. That
 * part of the original docblock reasoning below still holds.
 *
 * CORRECTED 2026-08-16: the original version of this docblock concluded
 * "no cross-tenant isolation to test" from that fact alone — but both
 * actions JOIN the tenant-owned `silver.reports` table to attach
 * title/company/commodity/filing_date, and until this fix neither action
 * checked whether the caller could see that joined report. That was a
 * real, live cross-tenant IDOR (also independently flagged, unfixed, in
 * the codebase's own 2026_08_15_030000_close_rls_admin_escape_hatch_third_
 * pass migration docblock). The fix and its regression coverage — mocked,
 * since public_geo.* isn't available under SQLite — live in
 * EntityReferencesControllerTest::
 * test_for_entity_scopes_joined_reports_to_callers_accessible_projects and
 * ::test_for_document_returns_404_when_report_belongs_to_inaccessible_project.
 *
 * The controller:
 *   - Validates canonical_type against a fixed whitelist (returns 404 on unknown type).
 *   - Validates pg_id and report_id as UUIDs (returns 400 on malformed input).
 *   - Returns an empty `documents: []` payload for a valid UUID that has no links.
 *   - Scopes the joined silver.reports row to the caller's project memberships.
 *
 * This file's IDOR surface is limited to what's testable without a real
 * Postgres public_geo schema:
 *   1. Unauthenticated access must be denied (401).
 *   2. An invalid canonical_type returns 404 (not 500).
 *   3. A malformed UUID returns 400 (not 500).
 *   4. A valid UUID with no links returns a graceful empty payload (not a data leak).
 * The actual cross-tenant report-scoping tests live in
 * EntityReferencesControllerTest (see above) since they need to assert on
 * mocked query construction, which this file's real-request style doesn't
 * do.
 *
 * Verified routes:
 *   GET /api/v1/public-geoscience/entities/{canonical_type}/{pg_id}/references
 *   GET /api/v1/public-geoscience/documents/{report_id}/references
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
