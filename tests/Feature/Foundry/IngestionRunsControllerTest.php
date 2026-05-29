<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\Str;
use Inertia\Testing\AssertableInertia;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * IngestionRunsController — per-project ingestion progress surface.
 *
 * Postgres-only: silver.reports + silver.document_passages live in the
 * pgsql test DB only. Run with:
 *   php artisan test -c phpunit.pgsql.xml --filter=IngestionRunsControllerTest
 */
final class IngestionRunsControllerTest extends TestCase
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
        $slug = 'ing-runs-'.substr($this->workspaceId, 0, 8);

        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$this->workspaceId, 'Ingest Runs Workspace', $slug],
        );

        $this->project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $this->project->project_id],
        );
        $this->user->projects()->syncWithoutDetaching([
            $this->project->project_id => ['role' => 'owner'],
        ]);

        // Fake the s3-bronze disk so listUploads() returns an empty list
        // rather than hitting a real MinIO. We exercise the in-flight branch
        // separately by faking files on the disk.
        Storage::fake('s3-bronze');
    }

    private function insertReport(string $title, int $passages, int $embedded): string
    {
        $reportId = (string) Str::uuid();
        DB::table('silver.reports')->insert([
            'report_id' => $reportId,
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->project->project_id,
            'title' => $title,
            'parser_used' => 'fitz',
            'parse_quality_pct' => 42.5,
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
                'embedding_id' => $i < $embedded ? "qdrant:abc:{$i}" : null,
            ]);
        }

        return $reportId;
    }

    public function test_show_redirects_outsider_to_403_or_404(): void
    {
        $outsider = User::factory()->create();

        $this->actingAs($outsider)
            ->get("/projects/{$this->project->slug}/ingestion-runs")
            ->assertStatus(404);
    }

    public function test_show_renders_inertia_page_with_completed_reports(): void
    {
        $this->insertReport('NI 43-101 Madsen PFS', passages: 100, embedded: 100);
        $this->insertReport('Corporate Presentation', passages: 20, embedded: 10);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/ingestion-runs")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->component('Foundry/IngestionRuns')
                    ->where('project.slug', $this->project->slug)
                    ->where('runs.totals.completed', 2)
                    ->where('runs.totals.in_flight', 0)
                    ->has('runs.completed', 2),
            );
    }

    public function test_progress_endpoint_returns_json_snapshot(): void
    {
        $this->insertReport('Madsen Technical Report', passages: 50, embedded: 50);

        $this->actingAs($this->user)
            ->getJson("/projects/{$this->project->slug}/ingestion-runs.json")
            ->assertOk()
            ->assertJsonStructure([
                'runs' => [
                    'in_flight',
                    'completed' => [
                        ['report_id', 'title', 'passages', 'embedded', 'embed_pct'],
                    ],
                    'totals' => ['in_flight', 'completed'],
                ],
                'fetched_at',
            ])
            ->assertJsonPath('runs.completed.0.embed_pct', 100)
            ->assertJsonPath('runs.completed.0.passages', 50);
    }

    public function test_progress_endpoint_classifies_unmatched_minio_files_as_in_flight(): void
    {
        // Upload a fake bronze object that has NO matching report row.
        $key = "reports/{$this->project->project_id}/20260524_120000_Madsen_NI43-101.pdf";
        Storage::disk('s3-bronze')->put($key, 'fake-pdf-bytes');

        $this->actingAs($this->user)
            ->getJson("/projects/{$this->project->slug}/ingestion-runs.json")
            ->assertOk()
            ->assertJsonPath('runs.totals.in_flight', 1)
            ->assertJsonPath('runs.in_flight.0.filename', '20260524_120000_Madsen_NI43-101.pdf');
    }

    public function test_progress_endpoint_surfaces_real_step_progress_from_ingest_progress_table(): void
    {
        // Phase B — a row in silver.ingest_progress should appear in in_flight
        // with progress_pct derived from step_index / total_steps and the
        // pretty step name surfaced as the stage.
        $key = "reports/{$this->project->project_id}/20260524_990000_BigPdf.pdf";
        DB::table('silver.ingest_progress')->insert([
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->project->project_id,
            'minio_key' => $key,
            'filename' => 'BigPdf.pdf',
            'current_step' => 'parse',
            'step_index' => 2,
            'total_steps' => 5,
            'started_at' => now(),
            'updated_at' => now(),
        ]);

        $this->actingAs($this->user)
            ->getJson("/projects/{$this->project->slug}/ingestion-runs.json")
            ->assertOk()
            ->assertJsonPath('runs.totals.in_flight', 1)
            ->assertJsonPath('runs.in_flight.0.stage', 'parse')
            ->assertJsonPath('runs.in_flight.0.step_index', 2)
            ->assertJsonPath('runs.in_flight.0.total_steps', 5)
            ->assertJsonPath('runs.in_flight.0.progress_pct', 40)
            ->assertJsonPath('runs.in_flight.0.has_real_progress', true);
    }

    public function test_progress_row_marked_completed_drops_out_of_in_flight(): void
    {
        $key = "reports/{$this->project->project_id}/20260524_990000_Done.pdf";
        DB::table('silver.ingest_progress')->insert([
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->project->project_id,
            'minio_key' => $key,
            'filename' => 'Done.pdf',
            'current_step' => 'completed',
            'step_index' => 5,
            'total_steps' => 5,
            'started_at' => now(),
            'updated_at' => now(),
            'completed_at' => now(),
        ]);

        $this->actingAs($this->user)
            ->getJson("/projects/{$this->project->slug}/ingestion-runs.json")
            ->assertOk()
            ->assertJsonPath('runs.totals.in_flight', 0);
    }

    public function test_progress_matches_minio_file_to_completed_report_by_filename(): void
    {
        // Report title should fuzzy-match the filename stem.
        $this->insertReport('Madsen NI 43-101 Final', passages: 10, embedded: 10);
        $key = "reports/{$this->project->project_id}/20260524_120000_Madsen_NI_43-101_Final.pdf";
        Storage::disk('s3-bronze')->put($key, 'fake-pdf');

        $this->actingAs($this->user)
            ->getJson("/projects/{$this->project->slug}/ingestion-runs.json")
            ->assertOk()
            ->assertJsonPath('runs.totals.in_flight', 0)
            ->assertJsonPath('runs.totals.completed', 1);
    }
}
