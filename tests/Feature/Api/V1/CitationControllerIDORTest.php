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

    private string $projectAId;

    private string $projectBId;

    protected function setUp(): void
    {
        parent::setUp();

        $this->workspaceA = (string) Str::uuid();
        $this->workspaceB = (string) Str::uuid();

        // silver.workspaces only exists under real Postgres — its CREATE
        // TABLE (UUID type + gen_random_uuid() default) is no-op'd by the
        // SQLite compatibility shim in Tests\TestCase, and the
        // silver.projects.workspace_id FK added by
        // 2026_04_20_100000_create_workspaces_and_data_version is a no-op
        // there too. Under real Postgres, `projects_workspace_id_fkey`
        // requires a matching silver.workspaces row before the
        // silver.projects UPDATE below (SQLSTATE 23503 otherwise).
        if (DB::connection()->getDriverName() !== 'sqlite') {
            DB::table('silver.workspaces')->insert([
                [
                    'workspace_id' => $this->workspaceA,
                    'name' => 'Citation IDOR Workspace A',
                    'slug' => 'citation-idor-a-'.substr($this->workspaceA, 0, 8),
                    'created_at' => now(),
                    'updated_at' => now(),
                ],
                [
                    'workspace_id' => $this->workspaceB,
                    'name' => 'Citation IDOR Workspace B',
                    'slug' => 'citation-idor-b-'.substr($this->workspaceB, 0, 8),
                    'created_at' => now(),
                    'updated_at' => now(),
                ],
            ]);
        }

        // Two users in two disjoint workspaces, following the
        // project_user membership pattern the controller scopes on.
        $this->userA = User::factory()->create();
        $projectA = Project::create([
            'project_name' => 'Workspace A Project '.uniqid(),
            'orientation_reference' => 'BOH',
        ]);
        $this->projectAId = (string) $projectA->project_id;
        $this->userA->projects()->attach($projectA->project_id, ['role' => 'owner']);
        DB::table('silver.projects')
            ->where('project_id', $projectA->project_id)
            ->update(['workspace_id' => $this->workspaceA]);

        $this->userB = User::factory()->create();
        $projectB = Project::create([
            'project_name' => 'Workspace B Project '.uniqid(),
            'orientation_reference' => 'BOH',
        ]);
        $this->projectBId = (string) $projectB->project_id;
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
            'project_id' => $this->projectBId,
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

    // -------------------------------------------------------------------------
    // Project scope: workspace membership is not enough
    // -------------------------------------------------------------------------

    /**
     * A second user in workspace B whose only project is a DIFFERENT project.
     */
    private function sameWorkspaceOtherProjectUser(): User
    {
        $user = User::factory()->create();
        $other = Project::create([
            'project_name' => 'Workspace B Other Project '.uniqid(),
            'orientation_reference' => 'BOH',
        ]);
        $user->projects()->attach($other->project_id, ['role' => 'owner']);
        DB::table('silver.projects')
            ->where('project_id', $other->project_id)
            ->update(['workspace_id' => $this->workspaceB]);

        return $user;
    }

    public function test_a_member_of_another_project_in_the_same_workspace_cannot_resolve_the_report(): void
    {
        $this->actingAs($this->sameWorkspaceOtherProjectUser(), 'sanctum');

        $response = $this->getJson($this->resolveUrl("georag_reports:{$this->reportBId}:section=1"));

        $response->assertNotFound();
        $this->assertStringNotContainsString('Confidential NI 43-101', json_encode($response->json()));
    }

    public function test_an_explicit_project_id_narrows_resolution_to_that_project(): void
    {
        // userB owns project B, so the report resolves with project B named...
        $this->actingAs($this->userB, 'sanctum');
        $this->getJson($this->resolveUrl("georag_reports:{$this->reportBId}:section=1").'&project_id='.$this->projectBId)
            ->assertOk();

        // ...and an explicit project the caller is NOT a member of matches nothing.
        $this->getJson($this->resolveUrl("georag_reports:{$this->reportBId}:section=1").'&project_id='.$this->projectAId)
            ->assertNotFound();
    }

    private function insertCollar(string $workspaceId, string $projectId, string $holeId): string
    {
        // The SQLite compatibility shim creates silver.collars without
        // workspace_id (and has no assays_v2); these tests need the real schema.
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('Requires the PostgreSQL silver schema (phpunit.pgsql.xml).');
        }

        $collarId = (string) Str::uuid();
        DB::table('silver.collars')->insert([
            'collar_id' => $collarId,
            'workspace_id' => $workspaceId,
            'project_id' => $projectId,
            'hole_id' => $holeId,
            'easting' => 500000,
            'northing' => 6000000,
            'hole_type' => 'DDH',
            'status' => 'completed',
        ]);

        return $collarId;
    }

    public function test_collar_citation_is_scoped_to_the_project(): void
    {
        $collarB = $this->insertCollar($this->workspaceB, $this->projectBId, 'PLS-B-01');
        $id = "silver.collars:count=1:first={$collarB}";

        $this->actingAs($this->sameWorkspaceOtherProjectUser(), 'sanctum');
        $this->getJson($this->resolveUrl($id))->assertNotFound();

        $this->actingAs($this->userB, 'sanctum');
        $this->getJson($this->resolveUrl($id))->assertOk()->assertJsonPath('metadata.hole_id', 'PLS-B-01');
    }

    public function test_lithology_citation_checks_the_collar_and_ignores_the_echoed_hole_name(): void
    {
        $collarB = $this->insertCollar($this->workspaceB, $this->projectBId, 'PLS-B-02');

        // Member of the owning project: resolves, and the title comes from the DB row.
        $this->actingAs($this->userB, 'sanctum');
        $this->getJson($this->resolveUrl("silver.lithology_logs:hole=SPOOFED:collar={$collarB}:intervals=3"))
            ->assertOk()
            ->assertJsonPath('title', 'Lithology Log: PLS-B-02');

        // Another project in the same workspace: 404.
        $this->actingAs($this->sameWorkspaceOtherProjectUser(), 'sanctum');
        $this->getJson($this->resolveUrl("silver.lithology_logs:hole=PLS-B-02:collar={$collarB}:intervals=3"))
            ->assertNotFound();

        // A collar that does not exist: 404, the supplied hole name is never echoed.
        $this->actingAs($this->userB, 'sanctum');
        $response = $this->getJson($this->resolveUrl('silver.lithology_logs:hole=MADE-UP-99:collar='.Str::uuid().':intervals=3'));
        $response->assertNotFound();
        $this->assertStringNotContainsString('MADE-UP-99', json_encode($response->json()['text'] ?? ''));
    }

    public function test_samples_citation_requires_the_element_to_exist_in_scope(): void
    {
        $collarB = $this->insertCollar($this->workspaceB, $this->projectBId, 'PLS-B-03');
        DB::table('silver.assays_v2')->insert([
            'id' => (string) Str::uuid(),
            'workspace_id' => $this->workspaceB,
            'collar_id' => $collarB,
            'sample_id' => 'S-1',
            'from_depth' => 1,
            'to_depth' => 2,
            'element' => 'U3O8',
            'unit' => 'ppm',
        ]);

        $this->actingAs($this->userB, 'sanctum');
        $this->getJson($this->resolveUrl('silver.samples:element=U3O8:count=4'))
            ->assertOk()
            ->assertJsonPath('metadata.element', 'U3O8');
        // An element no authorised data carries is not echoed back as fact.
        $this->getJson($this->resolveUrl('silver.samples:element=Unobtainium:count=4'))->assertNotFound();
        // Not an element at all.
        $this->getJson($this->resolveUrl('silver.samples:element=<script>:count=4'))->assertNotFound();

        // Same workspace, other project: the element is not in ITS data.
        $this->actingAs($this->sameWorkspaceOtherProjectUser(), 'sanctum');
        $this->getJson($this->resolveUrl('silver.samples:element=U3O8:count=4'))->assertNotFound();
    }

    public function test_assay_citation_is_scoped_to_the_project(): void
    {
        $collarB = $this->insertCollar($this->workspaceB, $this->projectBId, 'PLS-B-04');
        $assayId = (string) Str::uuid();
        DB::table('silver.assays_v2')->insert([
            'id' => $assayId,
            'workspace_id' => $this->workspaceB,
            'collar_id' => $collarB,
            'sample_id' => 'S-2',
            'from_depth' => 1,
            'to_depth' => 2,
            'element' => 'Au',
            'value' => 3.2,
            'unit' => 'g/t',
        ]);

        $this->actingAs($this->sameWorkspaceOtherProjectUser(), 'sanctum');
        $this->getJson($this->resolveUrl("silver.assays_v2:assay_id={$assayId}"))->assertNotFound();

        $this->actingAs($this->userB, 'sanctum');
        $this->getJson($this->resolveUrl("silver.assays_v2:assay_id={$assayId}"))->assertOk();
    }

    // -------------------------------------------------------------------------
    // Document-chunk citations resolve to the cited PASSAGE (by chunk=), not
    // to sections_text[<section=>]: the embedder writes the passage ordinal
    // into section=, so the old lookup showed an unrelated NI 43-101 section.
    // -------------------------------------------------------------------------

    public function test_a_passage_citation_resolves_to_the_cited_passage(): void
    {
        $passageId = $this->insertPassage($this->reportBId, $this->workspaceB, null, 4, 'Mineralisation at 312 m grades 2.1% U3O8.');

        $this->actingAs($this->userB, 'sanctum');
        $this->getJson($this->resolveUrl("georag_reports:{$this->reportBId}:section=4:chunk={$passageId}"))
            ->assertOk()
            ->assertJsonPath('text', 'Mineralisation at 312 m grades 2.1% U3O8.')
            ->assertJsonPath('section_title', 'Passage 5 (pp. 12–13)')
            ->assertJsonPath('section_number', null)
            ->assertJsonPath('metadata.report_id', $this->reportBId)
            ->assertJsonPath('metadata.page_first', 12);
    }

    public function test_a_passage_citation_is_still_scoped_to_the_tenant(): void
    {
        $passageId = $this->insertPassage($this->reportBId, $this->workspaceB, null, 4, 'Tenant B passage.');

        $this->actingAs($this->userA, 'sanctum');
        $response = $this->getJson($this->resolveUrl("georag_reports:{$this->reportBId}:section=4:chunk={$passageId}"));

        $response->assertNotFound();
        $this->assertStringNotContainsString('Tenant B passage', (string) $response->getContent());
    }

    public function test_a_structured_summary_citation_resolves_by_chunk_alone(): void
    {
        // ADR-0012 summaries have no parent report: their citation reads
        // `georag_reports:None:...`, which used to reach a uuid column and 500.
        $passageId = $this->insertPassage(null, $this->workspaceB, $this->projectBId, 0, 'Summary of 12 collars.');
        $url = $this->resolveUrl("georag_reports:None:section=0:chunk={$passageId}");

        $this->actingAs($this->userB, 'sanctum');
        $this->getJson($url)
            ->assertOk()
            ->assertJsonPath('text', 'Summary of 12 collars.');

        $this->actingAs($this->userA, 'sanctum');
        $this->getJson($url)->assertNotFound();
    }

    public function test_a_malformed_report_id_is_a_404_not_a_500(): void
    {
        $this->actingAs($this->userB, 'sanctum');
        $this->getJson($this->resolveUrl('georag_reports:not-a-uuid:section=1'))->assertNotFound();
    }

    public function test_malformed_collar_and_assay_ids_are_a_404_not_a_500(): void
    {
        // Each of these used to reach a uuid column and fail with 22P02.
        $this->actingAs($this->userB, 'sanctum');
        $this->getJson($this->resolveUrl('silver.collars:count=3:first=abc'))->assertNotFound();
        $this->getJson($this->resolveUrl('silver.assays_v2:assay_id='.str_repeat('-', 36)))->assertNotFound();
    }

    private function insertPassage(?string $reportId, string $workspaceId, ?string $projectId, int $ordinal, string $text): string
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('silver.document_passages page and project columns are Postgres-only.');
        }

        $passageId = (string) Str::uuid();
        DB::table('silver.document_passages')->insert([
            'passage_id' => $passageId,
            'document_id' => $reportId,
            'workspace_id' => $workspaceId,
            'project_id' => $projectId,
            'revision_number' => 1,
            'text' => $text,
            'text_hash' => hash('sha256', $text),
            'ordinal' => $ordinal,
            'embedding_id' => $passageId,
            'page_first' => $reportId === null ? null : 12,
            'page_last' => $reportId === null ? null : 13,
            'created_at' => now(),
            'updated_at' => now(),
        ]);

        return $passageId;
    }
}
