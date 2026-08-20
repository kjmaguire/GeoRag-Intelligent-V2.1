<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use App\Services\Dagster\DagsterGraphQLClient;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\UploadedFile;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\Str;
use Mockery;
use Mockery\MockInterface;
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
        Http::fake([
            '*' => Http::response(['errors' => null], 200),
        ]);
    }

    private function url(): string
    {
        return "/api/v1/projects/{$this->project->slug}/drill-uploads";
    }

    private function csv(string $name = 'collars.csv', string $content = "hole_id,east,north\nDH001,500000,6000000\n"): UploadedFile
    {
        return UploadedFile::fake()->createWithContent($name, $content);
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

    public function test_collar_csv_upload_persists_source_file_and_dispatches_silver_collars(): void
    {
        $this->mockDagsterDispatch('silver_collars');

        $response = $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('collars_2024.csv')]);

        $response
            ->assertCreated()
            ->assertJsonPath('route', 'dagster')
            ->assertJsonPath('asset_key', 'silver_collars')
            ->assertJsonPath('dispatch.dispatched', true);

        $sourceFileId = $response->json('source_file_id');
        $this->assertNotEmpty($sourceFileId);

        $row = DB::table('bronze.source_files')->where('id', $sourceFileId)->first();
        $this->assertNotNull($row, 'bronze.source_files row was not written');
        $this->assertSame($this->workspaceId, (string) $row->workspace_id);
        $this->assertSame('drill_upload', $row->source_type);
        $this->assertSame('silver_collars', $row->data_type);
        $this->assertStringStartsWith("drill-uploads/{$this->workspaceId}/", $row->seaweedfs_key);
        $this->assertStringEndsWith('_collars_2024.csv', $row->seaweedfs_key);

        $stored = Storage::disk('s3')->allFiles();
        $this->assertContains($row->seaweedfs_key, $stored);
    }

    public function test_lithology_csv_routes_to_silver_lithology(): void
    {
        $this->mockDagsterDispatch('silver_lithology');

        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('lithology_log.csv')])
            ->assertCreated()
            ->assertJsonPath('asset_key', 'silver_lithology');
    }

    public function test_sample_csv_routes_to_silver_samples(): void
    {
        $this->mockDagsterDispatch('silver_samples');

        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('assay_results.csv')])
            ->assertCreated()
            ->assertJsonPath('asset_key', 'silver_samples');
    }

    public function test_xlsx_routes_to_silver_xlsx(): void
    {
        $this->mockDagsterDispatch('silver_xlsx');

        $xlsx = UploadedFile::fake()->createWithContent('mixed.xlsx', 'stub-xlsx');

        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $xlsx])
            ->assertCreated()
            ->assertJsonPath('asset_key', 'silver_xlsx');
    }

    public function test_duplicate_sha256_returns_existing_row_without_re_uploading(): void
    {
        $this->mockDagsterDispatch('silver_collars');

        $payload = "hole_id,east,north\nDH002,1,2\n";
        $first = $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('collars_a.csv', $payload)])
            ->assertCreated();

        // Same content under a different filename — SHA matches, so we
        // expect a 200 + duplicate=true pointing at the original row.
        $second = $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('collars_b.csv', $payload)])
            ->assertOk()
            ->assertJsonPath('duplicate', true);

        $this->assertSame($first->json('source_file_id'), $second->json('source_file_id'));
        $this->assertCount(1, DB::table('bronze.source_files')
            ->where('workspace_id', $this->workspaceId)
            ->get(), 'a duplicate SHA must not create a second row');
    }

    public function test_dagster_route_dispatch_failure_returns_502_not_201(): void
    {
        // Distinct from test_unrouted_csv_still_persists_source_file: here
        // DrillAssetSelector DID classify the file (route='dagster',
        // asset_key='silver_collars') but the downstream Dagster call itself
        // failed — the exact shape of a Dagster-decommissioned deployment
        // (Phase B2 trim) receiving a classified upload. Previously this
        // still returned 201 with dispatched=false buried in the response,
        // reading as unqualified success while the row was never processed.
        $this->mock(DagsterGraphQLClient::class, function (MockInterface $m): void {
            $m->shouldReceive('launchAssetMaterialization')
                ->with('silver_collars', Mockery::type('array'))
                ->andReturn([
                    'dispatched' => false,
                    'run_id' => null,
                    'error' => 'connection_refused',
                ]);
        });

        $response = $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('collars_2024.csv')])
            ->assertStatus(502)
            ->assertJsonPath('route', 'dagster')
            ->assertJsonPath('dispatch.dispatched', false)
            ->assertJsonPath('error', 'ingestion_dispatch_failed');

        // The file must still be stored and the bronze row still written —
        // 502 signals "not processed yet", not "nothing happened".
        $sourceFileId = $response->json('source_file_id');
        $this->assertNotEmpty($sourceFileId);
        $this->assertNotNull(
            DB::table('bronze.source_files')->where('id', $sourceFileId)->first(),
            'the upload must still be durably stored even when dispatch fails',
        );
    }

    public function test_unrouted_csv_still_persists_source_file(): void
    {
        // No keyword — DrillAssetSelector returns route='unrouted'.
        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('random_data.csv')])
            ->assertCreated()
            ->assertJsonPath('route', 'unrouted')
            ->assertJsonPath('asset_key', null)
            ->assertJsonPath('dispatch.dispatched', false);

        $this->assertGreaterThan(
            0,
            DB::table('bronze.source_files')->where('workspace_id', $this->workspaceId)->count(),
            'an unrouted CSV must still anchor a bronze.source_files row',
        );
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

    /**
     * Replace the Dagster client with a mock that asserts the expected asset
     * key was launched, and returns a successful response.
     */
    private function mockDagsterDispatch(string $expectedAssetKey): void
    {
        $this->mock(DagsterGraphQLClient::class, function (MockInterface $m) use ($expectedAssetKey): void {
            $m->shouldReceive('launchAssetMaterialization')
                ->with($expectedAssetKey, Mockery::type('array'))
                ->andReturn([
                    'dispatched' => true,
                    'run_id' => 'mock-run-'.uniqid(),
                    'error' => null,
                ]);
        });
    }
}
