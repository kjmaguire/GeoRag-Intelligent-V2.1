<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use GuzzleHttp\Promise\PromiseInterface;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\Client\Request as ClientRequest;
use Illuminate\Http\UploadedFile;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * CC-01 Item 1 Slice 1 — DrillUploadController feature coverage.
 *
 * Postgres-only: writes to bronze.source_files which doesn't exist on
 * the SQLite fast suite (the bronze migration is gated on driver=pgsql).
 * Run with `php artisan test -c phpunit.pgsql.xml --filter=DrillUploadControllerTest`.
 */
class DrillUploadControllerTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private User $user;

    private Project $project;

    private string $workspaceId;

    /**
     * Per-test HTTP stubs, matched before setUp()'s default response.
     *
     * @var array<string, PromiseInterface>
     */
    private array $httpOverrides = [];

    protected function setUp(): void
    {
        parent::setUp();

        // silver.projects.workspace_id is only auto-populated by the
        // phase0 raw-SQL bootstrap in a real deployment (see
        // 2026_08_14_000000/025900) — a migrate-only Postgres test DB has
        // no such trigger, so it must be set explicitly here rather than
        // relying on it being auto-filled. workspace_id isn't fillable on
        // the Project model, so create the workspace + project, then
        // assign via a raw update — same pattern as
        // tests/Feature/Api/V1/IngestProgressControllerTest.php.
        $this->workspaceId = (string) Str::uuid();
        DB::table('silver.workspaces')->insert([
            'workspace_id' => $this->workspaceId,
            'name' => 'Drill Upload Test Workspace',
            'slug' => 'drill-upload-'.substr($this->workspaceId, 0, 8),
            'created_at' => now(),
            'updated_at' => now(),
        ]);

        $this->project = Project::create([
            'project_name' => 'Drill Upload Test '.uniqid(),
            'crs_datum' => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);
        DB::table('silver.projects')
            ->where('project_id', $this->project->project_id)
            ->update(['workspace_id' => $this->workspaceId]);

        $this->user = User::factory()->create();
        $this->user->projects()->attach($this->project->project_id, ['role' => 'owner']);

        Storage::fake('s3');

        // Http::fake() MERGES into the stub list and the FIRST matching
        // stub answers. A '*' catch-all registered here is therefore
        // unreachable-past: a test that later faked a 500 for
        // ingest_tabular got this 200 instead and asserted against a
        // success it never asked for. Route through $httpOverrides so a
        // per-test stub is consulted BEFORE the default, not after it.
        Http::fake(function (ClientRequest $request) {
            foreach ($this->httpOverrides as $pattern => $response) {
                if (Str::is($pattern, $request->url())) {
                    return $response;
                }
            }

            return Http::response(['errors' => null], 200);
        });
    }

    private function url(): string
    {
        return "/api/v1/projects/{$this->project->slug}/drill-uploads";
    }

    private function csv(string $name = 'collars.csv', string $content = "hole_id,east,north\nDH001,500000,6000000\n"): UploadedFile
    {
        return UploadedFile::fake()->createWithContent($name, $content);
    }

    private function pdf(string $name = 'report.pdf', ?string $content = null): UploadedFile
    {
        return UploadedFile::fake()->createWithContent(
            $name,
            $content ?? "%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n",
        );
    }

    public function test_unknown_slug_returns_404(): void
    {
        $this->actingAs($this->user)
            ->postJson('/api/v1/projects/this-slug-does-not-exist/drill-uploads', [
                'file' => $this->csv(),
            ])
            ->assertNotFound();
    }

    public function test_non_member_user_is_forbidden(): void
    {
        $outsider = User::factory()->create();

        $this->actingAs($outsider)
            ->postJson($this->url(), ['file' => $this->csv()])
            ->assertForbidden();
    }

    public function test_unsupported_extension_returns_422(): void
    {
        $jpg = UploadedFile::fake()->image('photo.jpg');

        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $jpg])
            ->assertStatus(422)
            ->assertJsonPath('error', 'unsupported_extension');
    }

    /**
     * 2026-08-17 CI-gap audit: this file predated the 2026-07-28 Dagster
     * retirement and, because CI never ran the Postgres-gated suite, the
     * drift went unnoticed. The controller then rejected every non-PDF
     * extension with a 422 `retired_pipeline`, and this file was rewritten
     * to assert that rejection.
     *
     * 2026-08-22: the rejection itself was the drift. `ingest_tabular`
     * shipped on 2026-08-20 and UploadController restored CSV/XLSX the same
     * day, so the drill-specific endpoint was the one surface still
     * refusing drill data. These tests cover the restored route.
     */
    public function test_collar_csv_dispatches_to_ingest_tabular_with_its_sheet_type(): void
    {
        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('collars_2024.csv')])
            ->assertStatus(201)
            ->assertJsonPath('route', 'hatchet_tabular')
            ->assertJsonPath('sheet_type', 'collar')
            ->assertJsonPath('dispatch.dispatched', true);

        Http::assertSent(function ($request) {
            return str_contains($request->url(), '/shadow/ingest_tabular/trigger')
                && ($request['sheet_type'] ?? null) === 'collar'
                && ($request['workspace_id'] ?? null) === $this->workspaceId;
        });

        $this->assertSame(
            1,
            DB::table('bronze.source_files')->where('workspace_id', $this->workspaceId)->count(),
            'the upload must still be anchored in bronze.source_files',
        );
    }

    public function test_a_workbook_is_dispatched_without_a_sheet_type(): void
    {
        // A workbook holds several tables. Pinning one type would make
        // ingest_tabular apply it to every sheet instead of classifying
        // each — so the key is omitted rather than sent as null.
        $xlsx = UploadedFile::fake()->createWithContent('mixed.xlsx', 'stub-xlsx');

        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $xlsx])
            ->assertStatus(201)
            ->assertJsonPath('route', 'hatchet_tabular')
            ->assertJsonPath('sheet_type', null);

        Http::assertSent(function ($request) {
            return str_contains($request->url(), '/shadow/ingest_tabular/trigger')
                && ! array_key_exists('sheet_type', $request->data());
        });
    }

    public function test_a_csv_with_no_filename_hint_is_still_dispatched(): void
    {
        // The old behaviour was 'unrouted': stored, never processed, 201.
        // ingest_tabular classifies from the header row, so there is no
        // reason to drop the file on the floor.
        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('data.csv')])
            ->assertStatus(201)
            ->assertJsonPath('route', 'hatchet_tabular')
            ->assertJsonPath('sheet_type', null)
            ->assertJsonPath('dispatch.dispatched', true);
    }

    public function test_a_failed_tabular_dispatch_is_a_502_not_a_quiet_201(): void
    {
        // The file IS stored, so a 201 reads as unqualified success while
        // the only signal is `dispatch.dispatched` three levels deep.
        $this->httpOverrides = [
            '*ingest_tabular*' => Http::response(['detail' => 'nope'], 500),
        ];

        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('surveys.csv')])
            ->assertStatus(502)
            ->assertJsonPath('error', 'ingestion_dispatch_failed')
            ->assertJsonPath('dispatch.dispatched', false);
    }

    public function test_source_epsg_reaches_the_ingest_tabular_trigger(): void
    {
        // IngestTabularInput has accepted `source_epsg` since it shipped and
        // has never once been sent one, so every drill file uploaded through
        // this route has silently taken DEFAULT_SOURCE_EPSG = 32613 (UTM
        // 13N). Correct in Saskatchewan; a continent out for the Alaskan
        // collars this override exists for (26904 = NAD83 / UTM 4N).
        $this->actingAs($this->user)
            ->postJson($this->url(), [
                'file' => $this->csv('collars_unga.csv'),
                'source_epsg' => 26904,
            ])
            ->assertStatus(201)
            ->assertJsonPath('source_epsg', 26904)
            ->assertJsonPath('dispatch.source_epsg', 26904);

        Http::assertSent(function ($request) {
            return str_contains($request->url(), '/shadow/ingest_tabular/trigger')
                && ($request['source_epsg'] ?? null) === 26904
                // Adding one hint must not displace the other.
                && ($request['sheet_type'] ?? null) === 'collar';
        });
    }

    public function test_source_epsg_is_omitted_when_not_supplied(): void
    {
        // Absence and null are not the same message. ingest_tabular reads a
        // missing key as "no operator assertion, use the default"; sending
        // the key as null says nothing extra and invites a future reader to
        // treat it as an explicit choice.
        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('collars_2024.csv')])
            ->assertStatus(201)
            ->assertJsonMissingPath('source_epsg');

        Http::assertSent(function ($request) {
            return str_contains($request->url(), '/shadow/ingest_tabular/trigger')
                && ! array_key_exists('source_epsg', $request->data());
        });
    }

    /**
     * What FastAPI's trigger endpoint writes at dispatch time. The fake HTTP
     * layer in these tests never reaches FastAPI, so a "the first attempt
     * really started" scenario has to seed the row itself.
     */
    private function seedIngestProgress(string $projectId, string $minioKey, string $status = 'started'): void
    {
        $runId = (string) Str::uuid();
        DB::table('silver.ingest_progress')->insert([
            'progress_id' => $runId,
            'run_id' => $runId,
            'workspace_id' => $this->workspaceId,
            'project_id' => $projectId,
            'minio_key' => $minioKey,
            'filename' => basename($minioKey),
            'current_step' => 'parse',
            'current_stage' => 'parse',
            'step_index' => 1,
            'total_steps' => 5,
            'status' => $status,
            'attempt_number' => 1,
            'triggered_by' => 'upload',
            'started_at' => now(),
            'updated_at' => now(),
        ]);
    }

    private function secondProject(): Project
    {
        $project = Project::create([
            'project_name' => 'Drill Upload Sibling '.uniqid(),
            'crs_datum' => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);
        DB::table('silver.projects')
            ->where('project_id', $project->project_id)
            ->update(['workspace_id' => $this->workspaceId]);
        $this->user->projects()->attach($project->project_id, ['role' => 'owner']);

        return $project;
    }

    private function triggerCount(): int
    {
        return Http::recorded(
            fn ($request) => str_contains($request->url(), '/trigger'),
        )->count();
    }

    public function test_duplicate_sha256_returns_existing_row_without_re_uploading(): void
    {
        $payload = "%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n";
        $first = $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->pdf('report_a.pdf', $payload)])
            ->assertCreated();

        // FastAPI inserts the ingest_progress row at dispatch time; the fake
        // HTTP layer does not, so seed what the real trigger would have.
        $this->seedIngestProgress($this->project->project_id, (string) $first->json('seaweedfs_key'));

        // Same content under a different filename — SHA matches, so we
        // expect a 200 + duplicate=true pointing at the original row.
        $second = $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->pdf('report_b.pdf', $payload)])
            ->assertOk()
            ->assertJsonPath('duplicate', true);

        $this->assertSame($first->json('source_file_id'), $second->json('source_file_id'));
        $this->assertCount(1, DB::table('bronze.source_files')
            ->where('workspace_id', $this->workspaceId)
            ->get(), 'a duplicate SHA must not create a second row');
        $this->assertSame(1, $this->triggerCount(), 'a true duplicate must not dispatch again');
    }

    public function test_the_same_file_into_a_second_project_is_ingested_not_short_circuited(): void
    {
        $sibling = $this->secondProject();

        $first = $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('collars_2024.csv')])
            ->assertCreated();
        $this->seedIngestProgress($this->project->project_id, (string) $first->json('seaweedfs_key'));

        $second = $this->actingAs($this->user)
            ->postJson("/api/v1/projects/{$sibling->slug}/drill-uploads", ['file' => $this->csv('collars_2024.csv')])
            ->assertCreated()
            ->assertJsonPath('dispatch.dispatched', true)
            ->assertJsonMissingPath('duplicate');

        // The first project's run owns the first key (ingest_progress is
        // unique per workspace + key), so the sibling gets its own object.
        $this->assertNotSame($first->json('seaweedfs_key'), $second->json('seaweedfs_key'));
        $this->assertSame($first->json('source_file_id'), $second->json('source_file_id'));
        $this->assertSame(2, $this->triggerCount());
        Http::assertSent(fn ($request) => str_contains($request->url(), '/trigger')
            && ($request['project_id'] ?? null) === $sibling->project_id
            && ($request['minio_key'] ?? null) === $second->json('seaweedfs_key'));
        $this->assertCount(1, DB::table('bronze.source_files')->where('workspace_id', $this->workspaceId)->get());

        // And once the sibling's run exists, a re-upload THERE is a duplicate.
        $this->seedIngestProgress($sibling->project_id, (string) $second->json('seaweedfs_key'));
        $this->actingAs($this->user)
            ->postJson("/api/v1/projects/{$sibling->slug}/drill-uploads", ['file' => $this->csv('collars_2024.csv')])
            ->assertOk()
            ->assertJsonPath('duplicate', true);
        $this->assertSame(2, $this->triggerCount());
    }

    public function test_a_retry_after_a_failed_dispatch_ingests_instead_of_reporting_a_duplicate(): void
    {
        $this->httpOverrides = [
            '*ingest_tabular*' => Http::response(['detail' => 'nope'], 500),
        ];
        $first = $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('collars_2024.csv')])
            ->assertStatus(502);

        // FastAPI is back. The bronze row exists but no run ever started.
        $this->httpOverrides = [];
        $retry = $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('collars_2024.csv')])
            ->assertCreated()
            ->assertJsonPath('dispatch.dispatched', true)
            ->assertJsonMissingPath('duplicate');

        // Nothing ran on the stored object, so it is reused, not re-uploaded.
        $this->assertSame($first->json('seaweedfs_key'), $retry->json('seaweedfs_key'));
        $this->assertSame($first->json('source_file_id'), $retry->json('source_file_id'));
        $this->assertCount(1, DB::table('bronze.source_files')->where('workspace_id', $this->workspaceId)->get());
    }

    public function test_a_failed_run_does_not_make_a_reupload_a_duplicate(): void
    {
        $first = $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('collars_2024.csv')])
            ->assertCreated();
        $this->seedIngestProgress($this->project->project_id, (string) $first->json('seaweedfs_key'), 'failed');

        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('collars_2024.csv')])
            ->assertStatus(201)
            ->assertJsonMissingPath('duplicate');
        $this->assertSame(2, $this->triggerCount());
    }

    public function test_persisted_mime_type_is_server_sniffed_not_client_declared(): void
    {
        // Security fix 2026-08-14 (MED): bronze.source_files.mime_type used
        // to store the attacker-controlled client-declared MIME. Upload a
        // real PDF while declaring a bogus client mime and assert the
        // sniffed value wins.
        $path = tempnam(sys_get_temp_dir(), 'georag_pdf_');
        file_put_contents(
            $path,
            "%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n",
        );
        $file = new UploadedFile($path, 'well_report.pdf', 'text/plain', null, true);

        $response = $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $file]);

        // 201 when the (faked) FastAPI dispatch succeeds, 502 when it does
        // not — either way the bronze row must already be persisted.
        $this->assertContains($response->status(), [201, 502]);

        $sourceFileId = $response->json('source_file_id');
        $this->assertNotEmpty($sourceFileId);

        $row = DB::table('bronze.source_files')->where('id', $sourceFileId)->first();
        $this->assertNotNull($row);
        $this->assertSame(
            'application/pdf',
            $row->mime_type,
            'mime_type must come from server-side content sniffing, not the client-declared value',
        );
    }

    public function test_a_concurrent_identical_upload_is_a_duplicate_not_a_second_dispatch(): void
    {
        $content = "hole_id,east,north\nDH001,500000,6000000\n";
        $sha = hash('sha256', $content);

        // Another request for the same bytes into this project is between
        // its bronze insert and FastAPI's ingest_progress write: it holds
        // the lock, and there is no progress row yet for the dedupe SELECT
        // to find.
        $held = Cache::lock("drill-upload:{$this->workspaceId}:{$this->project->project_id}:{$sha}", 60);
        $this->assertTrue($held->get());

        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('collars_2024.csv', $content)])
            ->assertOk()
            ->assertJsonPath('duplicate', true);

        $this->assertSame(0, $this->triggerCount(), 'a concurrent duplicate must not dispatch');
        $this->assertCount(0, DB::table('bronze.source_files')->where('workspace_id', $this->workspaceId)->get());

        // Once the first request finishes, the lock is free again.
        $held->release();
        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('collars_2024.csv', $content)])
            ->assertCreated();
        $this->assertSame(1, $this->triggerCount());
    }

    public function test_the_upload_lock_is_released_after_the_request(): void
    {
        $content = "hole_id,east,north\nDH001,500000,6000000\n";
        $sha = hash('sha256', $content);

        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('collars_2024.csv', $content)])
            ->assertCreated();

        $lock = Cache::lock("drill-upload:{$this->workspaceId}:{$this->project->project_id}:{$sha}", 60);
        $this->assertTrue($lock->get(), 'the lock must be released when the request ends');
        $lock->release();
    }

    /**
     * Make the bronze.source_files INSERT fail once, as a unique-violation
     * loser would, after (optionally) landing the winner's row.
     *
     * @param string|null $winnerKey null = no winner row; 'SAME' = the winner
     *                               minted the very key the loser did
     */
    private function failBronzeInsertOnce(string $content, ?string $winnerKey): void
    {
        $armed = true;
        $workspaceId = $this->workspaceId;
        $userId = $this->user->id;
        DB::beforeExecuting(function (string $query, array $bindings) use (&$armed, $content, $winnerKey, $workspaceId, $userId): void {
            if (! $armed || ! str_starts_with($query, 'insert into "bronze"."source_files"')) {
                return;
            }
            $armed = false;
            $mintedKey = collect($bindings)->first(
                fn ($b) => is_string($b) && str_starts_with($b, 'drill-uploads/'),
            );
            if ($winnerKey !== null) {
                DB::table('bronze.source_files')->insert([
                    'id' => (string) Str::uuid(),
                    'workspace_id' => $workspaceId,
                    'seaweedfs_key' => $winnerKey === 'SAME' ? $mintedKey : $winnerKey,
                    'original_filename' => 'winner.csv',
                    'file_sha256' => hash('sha256', $content),
                    'file_size_bytes' => strlen($content),
                    'mime_type' => 'text/csv',
                    'source_type' => 'drill_upload',
                    'data_type' => 'collar',
                    'campaign_id' => null,
                    'ingested_by' => (string) $userId,
                    'ingested_at' => now(),
                ]);
            }

            throw new \RuntimeException('simulated unique violation');
        });
    }

    public function test_a_losing_racer_does_not_delete_the_winners_object(): void
    {
        $content = "hole_id,east,north\nDH001,500000,6000000\n";
        // Same bytes, same second -> the winner minted the very same key.
        $this->failBronzeInsertOnce($content, 'SAME');

        $response = $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('collars_2024.csv', $content)])
            ->assertOk()
            ->assertJsonPath('duplicate', true);

        $this->assertTrue(
            Storage::disk('s3')->exists((string) $response->json('seaweedfs_key')),
            'the object the winner row points at must survive the loser\'s catch',
        );
        $this->assertSame(0, $this->triggerCount());
    }

    public function test_a_losing_racer_deletes_the_object_it_created_itself(): void
    {
        $content = "hole_id,east,north\nDH001,500000,6000000\n";
        $winnerKey = 'drill-uploads/'.$this->workspaceId.'/winner_key.csv';
        $this->failBronzeInsertOnce($content, $winnerKey);

        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('collars_2024.csv', $content)])
            ->assertOk()
            ->assertJsonPath('duplicate', true)
            ->assertJsonPath('seaweedfs_key', $winnerKey);

        $this->assertSame(
            [],
            Storage::disk('s3')->allFiles('drill-uploads'),
            'the loser\'s own, now-unreferenced object is removed',
        );
    }

    public function test_a_failed_insert_with_no_winner_removes_the_orphan_and_500s(): void
    {
        $content = "hole_id,east,north\nDH001,500000,6000000\n";
        $this->failBronzeInsertOnce($content, null);

        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('collars_2024.csv', $content)])
            ->assertStatus(500)
            ->assertJsonPath('error', 'persist_failed');

        $this->assertSame([], Storage::disk('s3')->allFiles('drill-uploads'));
    }
}
