<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
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
        // with the pretty step name surfaced as the stage.
        //
        // progress_pct is COMPLETED steps, not the current step's index:
        //
        //     (max(0, step_index - 1) + stage_pct) / total_steps
        //
        // On step 2 of 5 with no sub-step progress reported, exactly one step
        // is finished — 20%, not 40%. This asserted 40 until 2026-08-20,
        // matching the older step-quantized formula the smooth-bar change
        // replaced; the assertion was never updated because
        // "Laravel (Pint + PHPUnit)" was permanently red on an unrelated
        // APP_KEY fault and nobody was reading it.
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
            ->assertJsonPath('runs.in_flight.0.progress_pct', 20)
            ->assertJsonPath('runs.in_flight.0.has_real_progress', true);
    }

    public function test_progress_endpoint_blends_sub_step_progress_into_the_bar(): void
    {
        // The half of the smooth-bar formula the test above cannot reach:
        // stage_pct is the worker's fractional progress WITHIN the current
        // step (0..1, written by the page-level relay during a long parse).
        // Without a case that sets it, the `+ stage_pct` term could be
        // deleted and every existing assertion would still pass.
        //
        // Step 2 of 5, 60% through that step: (1 + 0.6) / 5 = 32%.
        $key = "reports/{$this->project->project_id}/20260524_990001_MidParse.pdf";
        DB::table('silver.ingest_progress')->insert([
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->project->project_id,
            'minio_key' => $key,
            'filename' => 'MidParse.pdf',
            'current_step' => 'parse',
            'step_index' => 2,
            'total_steps' => 5,
            'stage_pct' => 0.6,
            'started_at' => now(),
            'updated_at' => now(),
        ]);

        $this->actingAs($this->user)
            ->getJson("/projects/{$this->project->slug}/ingestion-runs.json")
            ->assertOk()
            ->assertJsonPath('runs.in_flight.0.progress_pct', 32);
    }

    public function test_a_clean_non_report_completion_stays_visible_but_uncounted(): void
    {
        // A shapefile / drill CSV / LAS completion has no silver.reports row,
        // so the "completed" card cannot show it. It used to be dropped from
        // in_flight too — success was the one outcome with no row anywhere on
        // the page. It now stays listed (rendered green) for the 24 h grace
        // window, while the IN FLIGHT total — which drives the stat tile and
        // the page's poll cadence — counts only rows still moving.
        $key = "spatial/{$this->project->project_id}/20260824_990000_geology.zip";
        DB::table('silver.ingest_progress')->insert([
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->project->project_id,
            'minio_key' => $key,
            'filename' => 'geology.zip',
            'current_step' => 'completed',
            'step_index' => 5,
            'total_steps' => 5,
            'status' => 'completed',
            'rows_written' => 170,
            'started_at' => now(),
            'updated_at' => now(),
            'completed_at' => now(),
        ]);

        $this->actingAs($this->user)
            ->getJson("/projects/{$this->project->slug}/ingestion-runs.json")
            ->assertOk()
            ->assertJsonPath('runs.totals.in_flight', 0)
            ->assertJsonPath('runs.in_flight.0.stage', 'completed')
            ->assertJsonPath('runs.in_flight.0.status', 'completed')
            ->assertJsonPath('runs.in_flight.0.rows_written', 170)
            // Not (n-1)/n: a finished run has no current step to be
            // fractionally inside of. "Completed · 80%" reads as stuck.
            ->assertJsonPath('runs.in_flight.0.progress_pct', 100);
    }

    public function test_a_clean_pdf_completion_is_not_listed_twice(): void
    {
        // A PDF completion carries its report_id on the progress row and is
        // rendered by the completed card (silver.reports). Listing it in the
        // runs card too would say everything twice.
        $reportId = $this->insertReport('Done Report', passages: 1, embedded: 1);
        $key = "reports/{$this->project->project_id}/20260524_990000_Done.pdf";
        DB::table('silver.ingest_progress')->insert([
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->project->project_id,
            'minio_key' => $key,
            'filename' => 'Done.pdf',
            'current_step' => 'completed',
            'step_index' => 5,
            'total_steps' => 5,
            'status' => 'completed',
            'report_id' => $reportId,
            'started_at' => now(),
            'updated_at' => now(),
            'completed_at' => now(),
        ]);

        $this->actingAs($this->user)
            ->getJson("/projects/{$this->project->slug}/ingestion-runs.json")
            ->assertOk()
            ->assertJsonPath('runs.totals.in_flight', 0)
            ->assertJsonCount(0, 'runs.in_flight')
            ->assertJsonPath('runs.totals.completed', 1);
    }

    public function test_a_legacy_completed_row_with_null_status_is_not_counted_as_moving(): void
    {
        // Rows from before the status column existed (2026-08-21) carry
        // current_step='completed' with a NULL status, which the mapper
        // reads as 'queued'. Counting those as in flight would pin the
        // 5-second fast poll on projects whose runs finished months ago.
        $key = "spatial/{$this->project->project_id}/20260524_990000_old.zip";
        DB::table('silver.ingest_progress')->insert([
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->project->project_id,
            'minio_key' => $key,
            'filename' => 'old.zip',
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

    public function test_poll_endpoint_and_page_load_agree_on_in_flight(): void
    {
        // Regression: 2f332f2 (2026-08-11) made the JSON poll skip the bronze
        // upload listing to save S3 calls. That broke the page. There is a real
        // window between upload and the first silver.ingest_progress row —
        // Laravel dispatches to Hatchet, and the row is only written by
        // FastAPI's _progress.start_run() once a worker picks the job up — and
        // during it the Phase-A fallback is the ONLY thing that surfaces the
        // file. So a just-uploaded file rendered on page load and then vanished
        // on the very next 5s poll, and because in_flight then read 0 the
        // frontend's own backoff dropped it to a 30s poll. Reloading brought it
        // back, which is the "I have to refresh it myself" symptom.
        //
        // The two endpoints must therefore agree. Asserting parity rather than
        // a literal count so this keeps holding as the snapshot shape evolves.
        $key = "reports/{$this->project->project_id}/20260819_101500_Parity_Check.pdf";
        Storage::disk('s3-bronze')->put($key, 'fake-pdf-bytes');

        $pageInFlight = null;
        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/ingestion-runs")
            ->assertOk()
            ->assertInertia(function (AssertableInertia $page) use (&$pageInFlight) {
                $pageInFlight = $page->toArray()['props']['runs']['totals']['in_flight'];
            });

        $poll = $this->actingAs($this->user)
            ->getJson("/projects/{$this->project->slug}/ingestion-runs.json")
            ->assertOk();

        $this->assertSame(
            1,
            $pageInFlight,
            'Page load should surface the unmatched bronze upload as in-flight.',
        );
        $this->assertSame(
            $pageInFlight,
            $poll->json('runs.totals.in_flight'),
            'The JSON poll disagreed with the page load on in_flight. A file '
            .'the user can see would disappear on the next poll — and the '
            .'frontend backs off from a 5s to a 30s poll when in_flight hits '
            .'0, so real progress would then take up to 30s to appear.',
        );
    }

    // ── Column remap ────────────────────────────────────────────────────
    //
    // The gap this closes: when alias matching misses a REQUIRED column the
    // whole file is refused, and the only remedy the app offered was
    // "rename the key columns and re-upload" — which asks a geologist to
    // edit their source data to suit our vocabulary, and which they cannot
    // do at all for a file they received from someone else.
    //
    // The bytes are already in bronze, so this re-triggers the same
    // workflow against the same object rather than re-uploading anything.

    public function test_a_confirmed_mapping_is_dispatched_to_the_workflow(): void
    {
        Http::fake([
            '*/internal/v1/shadow/ingest_tabular/trigger' => Http::response(
                ['workflow_run_id' => 'wfr-remap-1'], 202,
            ),
        ]);

        $this->actingAs($this->user)
            ->postJson("/projects/{$this->project->slug}/ingestion-runs/remap", [
                'minio_key' => "collars/{$this->project->project_id}/20260824_204518_assays.csv",
                'sheet_type' => 'collar',
                'column_map' => ['hole_id' => 'Site Ref', 'easting' => 'Grid Ref East'],
            ])
            ->assertStatus(202)
            ->assertJson(['dispatched' => true]);

        Http::assertSent(function ($request) {
            // Keyed by sheet_type: one workbook can map its collar sheet and
            // its lithology sheet differently.
            return $request['column_map'] === [
                'collar' => ['hole_id' => 'Site Ref', 'easting' => 'Grid Ref East'],
            ] && $request['sheet_type'] === 'collar';
        });
    }

    public function test_a_key_from_another_project_is_refused(): void
    {
        // The key arrives from the browser, and it is the only thing between
        // "re-run my file" and "ingest another project's object into mine".
        // RLS scopes the WRITES; the read would already have happened.
        Http::fake();

        $this->actingAs($this->user)
            ->postJson("/projects/{$this->project->slug}/ingestion-runs/remap", [
                'minio_key' => 'collars/'.Str::uuid().'/20260824_204518_someone_elses.csv',
                'sheet_type' => 'collar',
                'column_map' => ['hole_id' => 'Site Ref'],
            ])
            ->assertStatus(422);

        Http::assertNothingSent();
    }

    public function test_a_project_the_user_is_not_a_member_of_is_refused(): void
    {
        Http::fake();
        $outsider = User::factory()->create();

        $this->actingAs($outsider)
            ->postJson("/projects/{$this->project->slug}/ingestion-runs/remap", [
                'minio_key' => "collars/{$this->project->project_id}/x.csv",
                'sheet_type' => 'collar',
                'column_map' => ['hole_id' => 'Site Ref'],
            ])
            ->assertStatus(404);

        Http::assertNothingSent();
    }

    public function test_an_unknown_sheet_type_is_refused(): void
    {
        Http::fake();

        $this->actingAs($this->user)
            ->postJson("/projects/{$this->project->slug}/ingestion-runs/remap", [
                'minio_key' => "collars/{$this->project->project_id}/x.csv",
                'sheet_type' => 'geochemistry',
                'column_map' => ['hole_id' => 'Site Ref'],
            ])
            ->assertStatus(422);

        Http::assertNothingSent();
    }

    public function test_a_field_name_that_is_not_ours_is_refused(): void
    {
        // Canonical field names are the app's own vocabulary, not free text.
        Http::fake();

        $this->actingAs($this->user)
            ->postJson("/projects/{$this->project->slug}/ingestion-runs/remap", [
                'minio_key' => "collars/{$this->project->project_id}/x.csv",
                'sheet_type' => 'collar',
                'column_map' => ['DROP TABLE' => 'Site Ref'],
            ])
            ->assertStatus(422);

        Http::assertNothingSent();
    }

    public function test_an_empty_mapping_is_refused(): void
    {
        // Re-running with nothing mapped refuses the file a second time for
        // the same reason, which reads as "the mapping did not work".
        Http::fake();

        $this->actingAs($this->user)
            ->postJson("/projects/{$this->project->slug}/ingestion-runs/remap", [
                'minio_key' => "collars/{$this->project->project_id}/x.csv",
                'sheet_type' => 'collar',
                'column_map' => [],
            ])
            ->assertStatus(422);

        Http::assertNothingSent();
    }
}
