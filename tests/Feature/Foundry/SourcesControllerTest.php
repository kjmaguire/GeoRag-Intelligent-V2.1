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
 * SourcesController — the project "Data" surface.
 *
 * Regression coverage for the 2026-08-17 fix: every panel here used to be
 * driven by a bronze.provenance join keyed off a PLSS-section token that
 * only the historical bulk Wyoming archive import ever wrote. The live
 * ingest_pdf.py path never writes bronze.provenance, so passages/parser
 * activity/ingestion runs always showed empty even for a fully-ingested
 * PDF-only project. These tests seed data the way the live pipeline
 * actually writes it (silver.reports, silver.document_passages,
 * silver.ingest_progress — no bronze.* rows at all) and assert the page
 * reflects it.
 *
 * Postgres-only: silver.reports + silver.document_passages +
 * silver.ingest_progress live in the pgsql test DB only. Run with:
 *   php artisan test -c phpunit.pgsql.xml --filter=SourcesControllerTest
 */
final class SourcesControllerTest extends TestCase
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
        $slug = 'sources-'.substr($this->workspaceId, 0, 8);

        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$this->workspaceId, 'Sources Test Workspace', $slug],
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

    private function insertReportViaLivePipelineShape(string $title, int $passages, string $parserUsed = 'fitz'): string
    {
        $reportId = (string) Str::uuid();
        DB::table('silver.reports')->insert([
            'report_id' => $reportId,
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->project->project_id,
            'title' => $title,
            'parser_used' => $parserUsed,
            'parse_quality_pct' => 0.82,
            'is_scanned' => false,
            'version' => 1,
            'qp_name' => '{}',
        ]);

        for ($i = 0; $i < $passages; $i++) {
            DB::table('silver.document_passages')->insert([
                'passage_id' => (string) Str::uuid(),
                'document_id' => $reportId,
                'workspace_id' => $this->workspaceId,
                'revision_number' => 1,
                'text' => "passage {$i} of {$title}",
                'text_hash' => str_pad((string) $i, 64, '0', STR_PAD_LEFT),
                'ordinal' => $i,
            ]);
        }

        // Deliberately NO bronze.provenance row — this is the whole point:
        // the live ingest_pdf.py path never writes one.
        return $reportId;
    }

    public function test_show_redirects_outsider_to_404(): void
    {
        $outsider = User::factory()->create();

        $this->actingAs($outsider)
            ->get("/projects/{$this->project->slug}/sources")
            ->assertStatus(404);
    }

    public function test_passages_count_reflects_reports_with_no_bronze_provenance(): void
    {
        $this->insertReportViaLivePipelineShape('NI 43-101 Madsen PFS', passages: 12);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/sources")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->component('Foundry/Sources')
                    ->where('stats.reports_in_project', 1)
                    ->where('stats.passages_in_project', 12)
                    ->where('empty', false),
            );
    }

    public function test_parser_activity_derived_from_reports_parser_used(): void
    {
        $this->insertReportViaLivePipelineShape('Report A', passages: 3, parserUsed: 'fitz');
        $this->insertReportViaLivePipelineShape('Report B', passages: 2, parserUsed: 'fitz');
        $this->insertReportViaLivePipelineShape('Report C', passages: 1, parserUsed: 'pdfplumber');

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/sources")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('stats.parsers_active', 2)
                    ->where('parser_activity.0.parser', 'fitz')
                    ->where('parser_activity.0.rows_written', 2),
            );
    }

    public function test_recent_runs_sourced_from_ingest_progress_not_bronze(): void
    {
        DB::table('silver.ingest_progress')->insert([
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->project->project_id,
            'minio_key' => "reports/{$this->project->project_id}/20260817_000000_Test.pdf",
            'filename' => 'Test.pdf',
            'current_step' => 'completed',
            'status' => 'completed',
            'step_index' => 5,
            'total_steps' => 5,
            'started_at' => now(),
            'updated_at' => now(),
            'completed_at' => now(),
        ]);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/sources")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('stats.ingest_runs_in_project', 1)
                    ->where('recent_runs.0.source_path', 'Test.pdf')
                    ->where('recent_runs.0.status', 'completed')
                    ->where('file_types.0.kind', 'pdf')
                    ->where('file_types.0.count', 1),
            );
    }

    public function test_failed_run_surfaces_error_text(): void
    {
        DB::table('silver.ingest_progress')->insert([
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->project->project_id,
            'minio_key' => "reports/{$this->project->project_id}/20260817_000000_Broken.pdf",
            'filename' => 'Broken.pdf',
            'current_step' => 'failed',
            'status' => 'failed',
            'step_index' => 3,
            'total_steps' => 5,
            'started_at' => now(),
            'updated_at' => now(),
            'failed_at' => now(),
            'error_text' => 'InsufficientPrivilegeError: permission denied for table document_passages',
        ]);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/sources")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('recent_runs.0.status', 'failed')
                    ->where(
                        'recent_runs.0.error_text',
                        'InsufficientPrivilegeError: permission denied for table document_passages',
                    ),
            );
    }

    public function test_quality_score_derived_from_reports_parse_quality_pct(): void
    {
        $this->insertReportViaLivePipelineShape('Report A', passages: 1);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/sources")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('stats.avg_quality_score', 0.82),
            );
    }

    public function test_empty_project_shows_empty_state(): void
    {
        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/sources")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('empty', true),
            );
    }
}
