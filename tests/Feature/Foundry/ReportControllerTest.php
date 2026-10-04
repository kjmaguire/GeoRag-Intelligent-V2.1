<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Inertia\Testing\AssertableInertia;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * ReportController — the merged documents surface (list + reader + quality).
 *
 * Regression coverage for the 2026-08-18 fix. The passages query used to
 * reverse-look-up passages through two bronze.provenance rows (one with
 * target_table='document_passages', joined to one with
 * target_table='reports' on a shared source_file). ingest_pdf.py writes
 * NEITHER — it emits audit.audit_ledger rows, not bronze.provenance — so
 * the join matched nothing and a fully-ingested report with hundreds of
 * embedded passages rendered "No indexed passages for this report."
 *
 * Same root cause as the 2026-08-17 SourcesController fix; this controller
 * was missed in that pass. These tests seed exactly what the live pipeline
 * writes — silver.reports + silver.document_passages keyed by document_id,
 * and no bronze.* rows at all.
 *
 * Also locks the 2026-08-18 merge: /reports and /reports/{id} both render
 * Foundry/Reports, the master list is present either way, and selecting a
 * document is what adds the detail props.
 *
 * Postgres-only. Run with:
 *   php artisan test -c phpunit.pgsql.xml --filter=ReportControllerTest
 */
final class ReportControllerTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private User $user;

    private Project $project;

    private string $workspaceId;

    protected function setUp(): void
    {
        parent::setUp();

        $this->user = User::factory()->create();
        $this->workspaceId = (string) Str::uuid();
        $slug = 'reader-'.substr($this->workspaceId, 0, 8);

        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$this->workspaceId, 'Reader Test Workspace', $slug],
        );

        $this->project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $this->project->project_id],
        );
        $this->user->projects()->syncWithoutDetaching([
            $this->project->project_id => ['role' => 'owner'],
        ]);
    }

    /**
     * Seed a report the way ingest_pdf.py does: passages carry
     * document_id = report_id, and there is no bronze.provenance row.
     */
    private function insertReportViaLivePipelineShape(int $passages): string
    {
        $reportId = (string) Str::uuid();
        DB::table('silver.reports')->insert([
            'report_id' => $reportId,
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->project->project_id,
            'title' => 'NI 43-101 Madsen PFS',
            'parser_used' => 'fitz',
            'parse_quality_pct' => 0.6471,
            'is_scanned' => true,
            'version' => 1,
            'qp_name' => '{}',
        ]);

        for ($i = 0; $i < $passages; $i++) {
            $this->insertPassage($reportId, ordinal: $i, chunkKind: 'narrative', text: "passage {$i} body");
        }

        return $reportId;
    }

    private function insertPassage(
        string $reportId,
        int $ordinal,
        string $chunkKind,
        string $text,
    ): string {
        $passageId = (string) Str::uuid();
        DB::table('silver.document_passages')->insert([
            'passage_id' => $passageId,
            'document_id' => $reportId,
            'workspace_id' => $this->workspaceId,
            'revision_number' => 1,
            'text' => $text,
            'text_hash' => hash('sha256', $reportId.$chunkKind.$ordinal),
            'ordinal' => $ordinal,
            'chunk_kind' => $chunkKind,
            'page_first' => $ordinal + 1,
            'page_last' => $ordinal + 1,
        ]);

        return $passageId;
    }

    /**
     * The reader's ORIGINAL tab exists so a geologist can check extracted
     * text against the page it came from. These pin the three states it can
     * be in, because two of them look identical from the outside if you only
     * assert on the happy path: a document with no recorded source is NOT
     * the same as a document that does not exist, and conflating them would
     * have the UI claim a filing is missing when only the link is.
     */
    public function test_source_reports_unavailable_when_no_object_key_was_recorded(): void
    {
        $reportId = $this->insertReportViaLivePipelineShape(passages: 1);

        $response = $this->actingAs($this->user)
            ->getJson('/projects/'.$this->project->slug.'/reports/'.$reportId.'/source');

        $response->assertOk();
        $response->assertJsonPath('available', false);
        $response->assertJsonPath('reason', 'no_source_object_recorded');
    }

    public function test_has_source_is_false_until_an_object_key_exists(): void
    {
        $reportId = $this->insertReportViaLivePipelineShape(passages: 1);

        $response = $this->actingAs($this->user)
            ->get('/projects/'.$this->project->slug.'/reports/'.$reportId);

        $response->assertOk();
        $response->assertInertia(fn (AssertableInertia $page) => $page
            ->where('report.has_source', false),
        );
    }

    public function test_has_source_is_true_once_the_ingest_records_the_bronze_key(): void
    {
        $reportId = $this->insertReportViaLivePipelineShape(passages: 1);
        DB::table('silver.reports')
            ->where('report_id', $reportId)
            ->update(['source_object_key' => 'reports/'.$this->project->project_id.'/madsen-pfs.pdf']);

        $response = $this->actingAs($this->user)
            ->get('/projects/'.$this->project->slug.'/reports/'.$reportId);

        $response->assertOk();
        $response->assertInertia(fn (AssertableInertia $page) => $page
            ->where('report.has_source', true),
        );
    }

    /**
     * Cross-project probing must not distinguish "not yours" from "does not
     * exist" — same contract as figures(). A presigned URL is a bearer
     * credential for the object, so the membership check is the only thing
     * standing between a caller and another project's filing.
     */
    public function test_source_404s_for_a_report_in_another_project(): void
    {
        $otherProject = Project::factory()->create();
        $otherReportId = (string) Str::uuid();
        DB::table('silver.reports')->insert([
            'report_id' => $otherReportId,
            'workspace_id' => $this->workspaceId,
            'project_id' => $otherProject->project_id,
            'title' => 'Another project filing',
            'parser_used' => 'fitz',
            'version' => 1,
            'qp_name' => '{}',
            'source_object_key' => 'reports/'.$otherProject->project_id.'/secret.pdf',
        ]);

        $this->actingAs($this->user)
            ->getJson('/projects/'.$this->project->slug.'/reports/'.$otherReportId.'/source')
            ->assertNotFound();
    }

    public function test_outsider_cannot_open_the_reader(): void
    {
        $reportId = $this->insertReportViaLivePipelineShape(passages: 3);

        $this->actingAs(User::factory()->create())
            ->get("/projects/{$this->project->slug}/reports/{$reportId}")
            ->assertStatus(404);
    }

    public function test_passages_surface_for_a_report_with_no_bronze_provenance(): void
    {
        $reportId = $this->insertReportViaLivePipelineShape(passages: 5);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/reports/{$reportId}")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->component('Foundry/Reports')
                    ->has('passages', 5)
                    ->where('passages_total', 5)
                    ->where('passages.0.text', 'passage 0 body')
                    ->where('passages.0.ordinal', 0)
                    ->where('passages.0.chunk_kind', 'narrative')
                    ->where('empty', false),
            );
    }

    public function test_passages_are_capped_at_the_preview_limit_but_total_is_the_real_count(): void
    {
        // The live Madsen PFS report holds 952 passages; 42 is enough to
        // prove the cap without a slow seed.
        $reportId = $this->insertReportViaLivePipelineShape(passages: 42);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/reports/{$reportId}")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->component('Foundry/Reports')
                    ->has('passages', 30)
                    ->where('passages_total', 42),
            );
    }

    public function test_page_image_passages_sort_after_narrative_chunks(): void
    {
        // A page-image passage's ordinal is its PAGE number, so a plain
        // ordinal sort would interleave it among the narrative chunks.
        $reportId = $this->insertReportViaLivePipelineShape(passages: 3);
        $this->insertPassage($reportId, ordinal: 1, chunkKind: 'page_image', text: '[page 1 image]');

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/reports/{$reportId}")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->component('Foundry/Reports')
                    ->has('passages', 4)
                    ->where('passages_total', 4)
                    ->where('passages.0.chunk_kind', 'narrative')
                    ->where('passages.3.chunk_kind', 'page_image')
                    ->where('passages.3.text', '[page 1 image]'),
            );
    }

    public function test_report_with_no_passages_reports_zero(): void
    {
        $reportId = $this->insertReportViaLivePipelineShape(passages: 0);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/reports/{$reportId}")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->component('Foundry/Reports')
                    ->has('passages', 0)
                    ->where('passages_total', 0)
                    // `empty` is now a property of the PROJECT (no documents
                    // at all), not of the open document — this project has
                    // one document, it just has no passages.
                    ->where('empty', false)
                    ->where('reports.0.status', 'unassessed'),
            );
    }

    public function test_another_projects_report_is_not_readable_here(): void
    {
        $mine = $this->insertReportViaLivePipelineShape(passages: 2);

        $otherProject = Project::factory()->create();
        $otherReportId = (string) Str::uuid();
        DB::table('silver.reports')->insert([
            'report_id' => $otherReportId,
            'workspace_id' => $this->workspaceId,
            'project_id' => $otherProject->project_id,
            'title' => 'Another projects filing',
            'parser_used' => 'fitz',
            'version' => 1,
            'qp_name' => '{}',
        ]);
        $this->insertPassage($otherReportId, ordinal: 0, chunkKind: 'narrative', text: 'not this project');

        // Right project slug, wrong report → 404, not a cross-report read.
        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/reports/{$otherReportId}")
            ->assertStatus(404);

        // And my own report still shows only its own passages.
        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/reports/{$mine}")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->component('Foundry/Reports')
                    ->has('passages', 2)
                    ->where('passages_total', 2),
            );
    }

    public function test_index_renders_the_merged_surface_with_no_selection(): void
    {
        $this->insertReportViaLivePipelineShape(passages: 4);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/reports")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->component('Foundry/Reports')
                    ->has('reports', 1)
                    ->where('selected_id', null)
                    ->where('report', null)
                    ->has('passages', 0)
                    // Quality rollup and the corpus overview belong to the
                    // unselected state — they are what the /imports/quality
                    // and /corpus pages used to carry.
                    ->where('quality.documents', 1)
                    ->where('quality.passages_total', 4)
                    ->has('overview.recent_passages', 4),
            );
    }

    public function test_selecting_a_document_keeps_the_master_list(): void
    {
        $reportId = $this->insertReportViaLivePipelineShape(passages: 3);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/reports/{$reportId}")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->component('Foundry/Reports')
                    // The list is still there — that is the whole point of
                    // master-detail; the reader is no longer its own page.
                    ->has('reports', 1)
                    ->where('quality.documents', 1)
                    ->where('selected_id', $reportId)
                    ->where('report.report_id', $reportId)
                    // Deep-linking to a document skips the overview rather
                    // than computing a pane it will not render.
                    ->where('overview', null),
            );
    }

    public function test_quality_tab_data_rides_on_the_selected_documents_list_row(): void
    {
        // The reader's Quality tab reads passages/embedded off the master
        // list row rather than issuing its own per-document query, so the
        // list row has to carry them on a detail render too.
        $reportId = $this->insertReportViaLivePipelineShape(passages: 6);
        DB::table('silver.document_passages')
            ->where('document_id', $reportId)
            ->where('ordinal', '<', 4)
            ->update(['embedding_id' => 'qdrant:test:1']);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/reports/{$reportId}")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('reports.0.report_id', $reportId)
                    ->where('reports.0.passages', 6)
                    ->where('reports.0.embedded', 4)
                    ->where('reports.0.status', 'warn')
                    ->where('report.parser_used', 'fitz')
                    ->where('report.is_scanned', true),
            );
    }

    /**
     * The merge kept /imports/quality and /corpus as named redirects rather
     * than deleting them, so bookmarks, the chat citation links and any
     * route('foundry.ingest-quality') / route('foundry.corpus') caller keep
     * working. FoundryRoutesSmokeTest also covers these but skips itself when
     * no project exists, so the real assertion lives here.
     */
    public function test_merged_quality_route_redirects_to_the_reports_surface(): void
    {
        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/imports/quality")
            ->assertRedirect("/projects/{$this->project->slug}/reports");
    }

    public function test_merged_corpus_route_redirects_to_the_reports_surface(): void
    {
        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/corpus")
            ->assertRedirect("/projects/{$this->project->slug}/reports");
    }

    public function test_merged_routes_still_resolve_by_name(): void
    {
        // Callers use route() rather than hardcoded paths; the names must
        // survive the merge even though the controllers behind them are gone.
        $this->assertSame(
            "/projects/{$this->project->slug}/imports/quality",
            route('foundry.ingest-quality', ['slug' => $this->project->slug], false),
        );
        $this->assertSame(
            "/projects/{$this->project->slug}/corpus",
            route('foundry.corpus', ['slug' => $this->project->slug], false),
        );
    }

    // -----------------------------------------------------------------
    // Pagination + project-wide quality totals (2026-10)
    // -----------------------------------------------------------------

    /**
     * @return array<int, string> report ids, oldest first
     */
    private function seedReports(int $count, int $passagesEach, ?Project $project = null): array
    {
        $project ??= $this->project;
        $ids = [];
        for ($i = 0; $i < $count; $i++) {
            $reportId = (string) Str::uuid();
            DB::table('silver.reports')->insert([
                'report_id' => $reportId,
                'workspace_id' => $this->workspaceId,
                'project_id' => $project->project_id,
                'title' => "Filing {$i}",
                'parser_used' => 'fitz',
                'version' => 1,
                'qp_name' => '{}',
                'updated_at' => now()->subMinutes($count - $i),
            ]);
            for ($p = 0; $p < $passagesEach; $p++) {
                $this->insertPassage($reportId, ordinal: $p, chunkKind: 'narrative', text: "f{$i} p{$p}");
            }
            $ids[] = $reportId;
        }

        return $ids;
    }

    public function test_the_list_is_paginated_and_exposes_total_page_and_per_page(): void
    {
        $this->seedReports(5, passagesEach: 2);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/reports?per_page=2&page=2")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->component('Foundry/Reports')
                    ->has('reports', 2)
                    // newest first: page 2 of 2-per-page is the 3rd/4th newest
                    ->where('reports.0.title', 'Filing 2')
                    ->where('reports.1.title', 'Filing 1')
                    ->where('reports_pagination.total', 5)
                    ->where('reports_pagination.page', 2)
                    ->where('reports_pagination.per_page', 2)
                    ->where('reports_pagination.last_page', 3)
                    ->where('empty', false),
            );
    }

    public function test_the_default_page_size_is_unchanged_and_garbage_params_fall_back(): void
    {
        $this->seedReports(3, passagesEach: 1);

        foreach (['', '?page=abc&per_page=-4', '?page=0', '?per_page=99999999999'] as $query) {
            $this->actingAs($this->user)
                ->get("/projects/{$this->project->slug}/reports".$query)
                ->assertOk()
                ->assertInertia(
                    fn (AssertableInertia $page) => $page
                        ->has('reports', 3)
                        ->where('reports_pagination.page', 1)
                        ->where('reports_pagination.total', 3),
                );
        }

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/reports")
            ->assertInertia(fn (AssertableInertia $page) => $page->where('reports_pagination.per_page', 60));
    }

    public function test_quality_totals_cover_the_whole_project_not_the_visible_page(): void
    {
        $this->seedReports(5, passagesEach: 3);
        // A sibling project in the SAME workspace must not leak into totals.
        $sibling = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $sibling->project_id],
        );
        $this->seedReports(4, passagesEach: 7, project: $sibling);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/reports?per_page=2")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->has('reports', 2)
                    ->where('quality.documents', 5)
                    ->where('quality.passages_total', 15)
                    // embedding_id is NULL on every seeded passage: all five
                    // documents have passages and none are retrievable.
                    ->where('quality.embedded_total', 0)
                    ->where('quality.documents_not_retrievable', 5),
            );
    }

    public function test_an_empty_project_reports_empty_and_a_zero_total(): void
    {
        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/reports")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('empty', true)
                    ->where('reports_pagination.total', 0)
                    ->where('reports_pagination.last_page', 1)
                    ->where('quality.documents', 0),
            );
    }
}
