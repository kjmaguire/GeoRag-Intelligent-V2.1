<?php

namespace Tests\Feature\Api\V1;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

/**
 * Module 10 Chunk 10.3 — IDOR regression tests for CitationController.
 *
 * CitationController exposes only one route:
 *   GET /api/v1/citations/resolve?source_chunk_id=...
 *
 * The controller resolves source_chunk_ids against silver.reports, silver.collars,
 * and public_geoscience.* tables. It carries no project_id or user-scoping — any
 * authenticated user may call the resolve endpoint with any source_chunk_id.
 *
 * IDOR analysis:
 *   • The resolve endpoint does NOT scope to the caller's project. It is
 *     intentionally document-resolver-wide: the same NI 43-101 report chunk
 *     may be referenced by queries from multiple users/projects and must remain
 *     resolvable. The endpoint returns structured metadata, not raw PII.
 *   • Per the architecture contract (§04h, §07d), citations are workspace-global
 *     read-only lookup records whose IDs are already embedded in the streamed
 *     RAG answer payload — they are not secrets. An authenticated user knowing a
 *     source_chunk_id gains no privilege they could not have obtained from the
 *     broadcast RAG answer.
 *   • Public Geoscience chunk IDs (pg_mine:, pg_mineral_occurrence: etc.) refer
 *     to government-published open-data records and are not tenant-scoped by
 *     design.
 *
 * Result: CitationController has no IDOR surface requiring a deny-on-cross-tenant
 * test. Instead, this file validates that:
 *   1. The endpoint requires authentication (unauthenticated → 401).
 *   2. A valid authenticated request with a missing source_chunk_id → 400.
 *   3. A valid authenticated request with an unknown prefix → graceful 200
 *      with source_type=unknown (not a 500 or data leak).
 *
 * Verified routes: GET /api/v1/citations/resolve
 * All are workspace-global / user-read-accessible by design.
 * No project-scoped IDOR test required.
 */
class CitationControllerIDORTest extends TestCase
{
    use RefreshDatabase;

    private User $userA;

    protected function setUp(): void
    {
        parent::setUp();

        $this->userA = User::factory()->create();
    }

    // -------------------------------------------------------------------------
    // Auth gate: unauthenticated request must be rejected
    // -------------------------------------------------------------------------

    public function test_unauthenticated_resolve_returns_401(): void
    {
        $response = $this->getJson('/api/v1/citations/resolve?source_chunk_id=georag_reports:some-id');

        $response->assertUnauthorized();
    }

    // -------------------------------------------------------------------------
    // Validation: missing source_chunk_id → 400
    // -------------------------------------------------------------------------

    public function test_resolve_without_source_chunk_id_returns_400(): void
    {
        $this->actingAs($this->userA, 'sanctum');

        $response = $this->getJson('/api/v1/citations/resolve');

        $response->assertStatus(400);
    }

    // -------------------------------------------------------------------------
    // Graceful handling: unknown prefix returns 200 with source_type=unknown
    // (not a server error or data leak)
    // -------------------------------------------------------------------------

    public function test_resolve_unknown_prefix_returns_200_with_unknown_type(): void
    {
        $this->actingAs($this->userA, 'sanctum');

        $response = $this->getJson('/api/v1/citations/resolve?source_chunk_id=nonexistent_prefix:some-id');

        $response->assertOk()
            ->assertJsonPath('source_type', 'unknown');
    }
}
