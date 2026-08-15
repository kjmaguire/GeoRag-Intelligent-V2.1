<?php

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * IDOR regression tests for CitationController.
 *
 * GET /api/v1/citations/resolve?source_chunk_id=...
 *
 * Security fix 2026-08-14 (HIGH — cross-tenant IDOR): this endpoint used to
 * resolve any workspace's silver.reports / silver.collars / silver.assays_v2
 * content for any authenticated user — it never set the app.workspace_id RLS
 * GUC (silver RLS policies are fail-open when the GUC is unset) and the
 * resolvers did key-only lookups. The earlier revision of this test file
 * documented that as by-design; the 2026-08 security audit overturned it:
 * tenant-scoped corpus content (report section text, collar/assay records)
 * is NOT workspace-global.
 *
 * Contract now under test:
 *   1. Unauthenticated → 401.
 *   2. Missing source_chunk_id → 400.
 *   3. Unknown prefix → 200 with source_type=unknown (graceful empty state).
 *   4. A user in workspace A resolving workspace B's report_id → 404.
 *   5. The SAME 404 shape for a genuinely nonexistent report_id (no
 *      existence oracle: cross-tenant and missing are indistinguishable).
 *   6. A member of the owning workspace still resolves the report → 200.
 *   7. A user with no project memberships cannot resolve tenant content.
 *
 * Public Geoscience prefixes (pg_mine: etc.) remain workspace-global by
 * design — government open data, not tenant-scoped.
 *
 * On the SQLite fast suite this exercises the belt-and-braces explicit
 * `workspace_id` WHERE filters; the RLS GUC layer (SELECT set_config) is
 * no-op'd by the TestCase compatibility hook and is covered by the
 * Postgres-gated tenancy suite in CI.
 */
class CitationControllerIDORTest extends TestCase
{
    use RefreshDatabase;

    private User $userA;

    private User $userB;

    private string $workspaceA;

    private string $workspaceB;

    private string $reportBId;

    protected function setUp(): void
    {
        parent::setUp();

        $this->workspaceA = (string) Str::uuid();
        $this->workspaceB = (string) Str::uuid();

        // Two users in two disjoint workspaces, following the
        // project_user membership pattern the controller scopes on.
        $this->userA = User::factory()->create();
        $projectA = Project::create([
            'project_name' => 'Workspace A Project '.uniqid(),
            'orientation_reference' => 'BOH',
        ]);
        $this->userA->projects()->attach($projectA->project_id, ['role' => 'owner']);
        DB::table('silver.projects')
            ->where('project_id', $projectA->project_id)
            ->update(['workspace_id' => $this->workspaceA]);

        $this->userB = User::factory()->create();
        $projectB = Project::create([
            'project_name' => 'Workspace B Project '.uniqid(),
            'orientation_reference' => 'BOH',
        ]);
        $this->userB->projects()->attach($projectB->project_id, ['role' => 'owner']);
        DB::table('silver.projects')
            ->where('project_id', $projectB->project_id)
            ->update(['workspace_id' => $this->workspaceB]);

        // A report owned by workspace B.
        $this->reportBId = (string) Str::uuid();
        DB::table('silver.reports')->insert([
            'report_id' => $this->reportBId,
            'title' => 'Confidential NI 43-101 for Workspace B',
            'company' => 'Tenant B Mining Corp',
            'commodity' => 'uranium',
            'sections_text' => json_encode(['1' => 'Section 1 — summary text.']),
            'workspace_id' => $this->workspaceB,
            'created_at' => now(),
            'updated_at' => now(),
        ]);

        // The found-path summarises cross-corpus links from
        // public_geo.document_entity_links. That table is created via raw
        // PG-only SQL (no-op'd on SQLite) — provision a bare stand-in so
        // the same-workspace 200 path is exercisable on the fast suite.
        if (! Schema::hasTable('document_entity_links')) {
            Schema::create('document_entity_links', function ($table): void {
                $table->increments('id');
                $table->uuid('document_id');
                $table->string('canonical_type', 32);
                $table->uuid('entity_id')->nullable();
                $table->decimal('confidence', 4, 3)->default(0);
                $table->text('signals')->nullable();
                $table->timestamp('established_at')->nullable();
                $table->string('established_by', 64)->nullable();
                $table->timestamp('superseded_at')->nullable();
            });
        }
    }

    private function resolveUrl(string $sourceChunkId): string
    {
        return '/api/v1/citations/resolve?source_chunk_id='.urlencode($sourceChunkId);
    }

    // -------------------------------------------------------------------------
    // Auth gate: unauthenticated request must be rejected
    // -------------------------------------------------------------------------

    public function test_unauthenticated_resolve_returns_401(): void
    {
        $response = $this->getJson($this->resolveUrl('georag_reports:some-id'));

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
    // -------------------------------------------------------------------------

    public function test_resolve_unknown_prefix_returns_200_with_unknown_type(): void
    {
        $this->actingAs($this->userA, 'sanctum');

        $response = $this->getJson($this->resolveUrl('nonexistent_prefix:some-id'));

        $response->assertOk()
            ->assertJsonPath('source_type', 'unknown');
    }

    // -------------------------------------------------------------------------
    // IDOR: user in workspace A must NOT resolve workspace B's report → 404
    // -------------------------------------------------------------------------

    public function test_cross_tenant_report_resolve_returns_404(): void
    {
        $this->actingAs($this->userA, 'sanctum');

        $response = $this->getJson(
            $this->resolveUrl("georag_reports:{$this->reportBId}:section=1"),
        );

        $response->assertNotFound();

        // The response must not leak any content of workspace B's report.
        $body = json_encode($response->json());
        $this->assertStringNotContainsString('Confidential NI 43-101', $body);
        $this->assertStringNotContainsString('Tenant B Mining Corp', $body);
        $this->assertStringNotContainsString('Section 1 — summary text', $body);
    }

    // -------------------------------------------------------------------------
    // No existence oracle: nonexistent report_id yields the SAME 404 shape
    // -------------------------------------------------------------------------

    public function test_missing_report_indistinguishable_from_cross_tenant(): void
    {
        $this->actingAs($this->userA, 'sanctum');

        $crossTenant = $this->getJson(
            $this->resolveUrl("georag_reports:{$this->reportBId}:section=1"),
        );
        $missing = $this->getJson(
            $this->resolveUrl('georag_reports:'.Str::uuid().':section=1'),
        );

        $crossTenant->assertNotFound();
        $missing->assertNotFound();

        // Identical body apart from the echoed source_chunk_id.
        $a = $crossTenant->json();
        $b = $missing->json();
        unset($a['source_chunk_id'], $b['source_chunk_id']);
        $this->assertSame($a, $b);
    }

    // -------------------------------------------------------------------------
    // Regression guard: a member of the owning workspace still resolves → 200
    // -------------------------------------------------------------------------

    public function test_same_workspace_report_resolve_returns_200(): void
    {
        $this->actingAs($this->userB, 'sanctum');

        $response = $this->getJson(
            $this->resolveUrl("georag_reports:{$this->reportBId}:section=1"),
        );

        $response->assertOk()
            ->assertJsonPath('source_type', 'report')
            ->assertJsonPath('title', 'Confidential NI 43-101 for Workspace B')
            ->assertJsonPath('metadata.report_id', $this->reportBId);
    }

    // -------------------------------------------------------------------------
    // Fail closed: a user with NO project memberships gets 404, not data
    // -------------------------------------------------------------------------

    public function test_user_without_memberships_cannot_resolve_tenant_content(): void
    {
        $orphan = User::factory()->create();
        $this->actingAs($orphan, 'sanctum');

        $response = $this->getJson(
            $this->resolveUrl("georag_reports:{$this->reportBId}:section=1"),
        );

        $response->assertNotFound();
    }
}
