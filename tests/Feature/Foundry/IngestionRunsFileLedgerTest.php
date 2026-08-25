<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * The Ingestion Runs page must publish a number that reconciles.
 *
 * Before this, the two totals it exposed answered different questions and
 * neither was "how many of the files I uploaded got processed":
 *
 *     totals.in_flight   rows still MOVING — 0 once a delivery settles.
 *     totals.completed   silver.reports rows, i.e. DOCUMENTS. A drill CSV,
 *                        a shapefile bundle and a standalone .dbf produce no
 *                        report at all.
 *
 * A real 72-file delivery on 2026-08-25 therefore finished with the page
 * reading "0 in flight · 41 completed" and nothing on it that added up to
 * what had been dropped, which reads as thirty files silently lost. They
 * were not lost — 41 was the document count.
 *
 * `totals.files` is the honest denominator: `$progress` is already
 * `DISTINCT ON (minio_key)`, so it is one row per uploaded object, and the
 * five `files_*` counts partition it exactly. That last property is what the
 * tests below actually pin — a ledger whose parts do not sum to its total is
 * worse than no ledger.
 */
final class IngestionRunsFileLedgerTest extends TestCase
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

        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$this->workspaceId, 'Ledger Workspace', 'ledger-'.substr($this->workspaceId, 0, 8)],
        );

        $this->project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $this->project->project_id],
        );
        $this->user->projects()->syncWithoutDetaching([
            $this->project->project_id => ['role' => 'owner'],
        ]);

        // Same as IngestionRunsControllerTest: keep listUploads() off a real
        // object store so the ledger reflects ingest_progress alone.
        Storage::fake('s3-bronze');
    }

    /**
     * @param string $status One of the seven `ingest_progress_status_valid`
     *                       allows: queued, started, completed, partial,
     *                       failed, cancelled, timed_out. NOT 'running' —
     *                       a moving row carries status='started', and the
     *                       controller derives the "running" bucket from it.
     * @param string $step One of the eight `ingest_progress_step_valid`
     *                     allows. A `partial` run sets current_step
     *                     'completed' — it DID reach the end — which is
     *                     why partial and completed share a step here.
     */
    private function insertRun(string $filename, string $status, string $step = 'completed'): void
    {
        DB::table('silver.ingest_progress')->insert([
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->project->project_id,
            'minio_key' => "reports/{$this->project->project_id}/{$filename}",
            'filename' => $filename,
            'current_step' => $step,
            'step_index' => 5,
            'total_steps' => 5,
            'status' => $status,
            'started_at' => now(),
            'updated_at' => now(),
        ]);
    }

    public function test_every_outcome_is_counted_not_just_completed(): void
    {
        $this->insertRun('collars.csv', 'completed');
        $this->insertRun('survey.csv', 'completed');
        $this->insertRun('ages.xls', 'partial');
        $this->insertRun('plan.str', 'failed', 'failed');
        // A swept run keeps whatever step it died on; only `status` moves.
        $this->insertRun('big.tif', 'timed_out', 'parse');
        $this->insertRun('slow.pdf', 'started', 'parse');

        $this->actingAs($this->user)
            ->getJson("/projects/{$this->project->slug}/ingestion-runs.json")
            ->assertOk()
            ->assertJsonPath('runs.totals.files', 6)
            ->assertJsonPath('runs.totals.files_completed', 2)
            ->assertJsonPath('runs.totals.files_partial', 1)
            ->assertJsonPath('runs.totals.files_failed', 1)
            ->assertJsonPath('runs.totals.files_timed_out', 1)
            ->assertJsonPath('runs.totals.files_running', 1);
    }

    public function test_the_parts_sum_to_the_total(): void
    {
        // The invariant the whole ledger rests on. A status this controller
        // does not recognise must land in one of the five buckets rather
        // than vanishing — a ledger that silently drops a row is exactly the
        // failure it was added to fix.
        $steps = [
            'completed' => 'completed',
            'partial' => 'completed',
            'failed' => 'failed',
            'cancelled' => 'failed',
            'timed_out' => 'parse',
            'started' => 'parse',
            'queued' => 'queued',
        ];
        $i = 0;
        foreach ($steps as $status => $step) {
            $this->insertRun('file'.$i++.'.csv', $status, $step);
        }

        $totals = $this->actingAs($this->user)
            ->getJson("/projects/{$this->project->slug}/ingestion-runs.json")
            ->assertOk()
            ->json('runs.totals');

        $sum = $totals['files_completed']
            + $totals['files_partial']
            + $totals['files_failed']
            + $totals['files_timed_out']
            + $totals['files_running'];

        $this->assertSame(
            $totals['files'],
            $sum,
            'the per-status counts must partition totals.files exactly',
        );
    }

    public function test_cancelled_is_reported_as_failed_rather_than_dropped(): void
    {
        // 'cancelled' has no tile of its own — a geologist reading the page
        // does not need the distinction between "the worker gave up" and
        // "something cancelled it", only that the file did not land. What
        // matters is that it is not silently uncounted.
        $this->insertRun('gone.csv', 'cancelled', 'failed');

        $this->actingAs($this->user)
            ->getJson("/projects/{$this->project->slug}/ingestion-runs.json")
            ->assertOk()
            ->assertJsonPath('runs.totals.files', 1)
            ->assertJsonPath('runs.totals.files_failed', 1);
    }

    public function test_retries_of_one_file_count_once(): void
    {
        // ingest_progress carries one row per ATTEMPT. The query is
        // DISTINCT ON (minio_key), so a file that failed and was recovered
        // is one file, not two — otherwise a delivery that hit a few
        // retries would report more files than were ever uploaded.
        $key = "reports/{$this->project->project_id}/retried.csv";
        foreach ([['failed', 1], ['completed', 2]] as [$status, $attempt]) {
            DB::table('silver.ingest_progress')->insert([
                'workspace_id' => $this->workspaceId,
                'project_id' => $this->project->project_id,
                'minio_key' => $key,
                'filename' => 'retried.csv',
                'current_step' => $status === 'failed' ? 'failed' : 'completed',
                'step_index' => 5,
                'total_steps' => 5,
                'status' => $status,
                'attempt_number' => $attempt,
                'started_at' => now()->addSeconds($attempt),
                'updated_at' => now()->addSeconds($attempt),
            ]);
        }

        $this->actingAs($this->user)
            ->getJson("/projects/{$this->project->slug}/ingestion-runs.json")
            ->assertOk()
            ->assertJsonPath('runs.totals.files', 1)
            ->assertJsonPath('runs.totals.files_completed', 1)
            ->assertJsonPath('runs.totals.files_failed', 0);
    }

    public function test_a_project_with_no_uploads_reports_zero_files(): void
    {
        $this->actingAs($this->user)
            ->getJson("/projects/{$this->project->slug}/ingestion-runs.json")
            ->assertOk()
            ->assertJsonPath('runs.totals.files', 0);
    }
}
