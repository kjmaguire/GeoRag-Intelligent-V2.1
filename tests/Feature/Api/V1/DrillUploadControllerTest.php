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

        $this->project = Project::create([
            'project_name' => 'Drill Upload Test '.uniqid(),
            'crs_datum' => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);

        $this->user = User::factory()->create();
        $this->user->projects()->attach($this->project->project_id, ['role' => 'owner']);

        // The Project model belongs to a workspace via silver.projects.workspace_id.
        // The factory may auto-fill this; capture it so we can assert paths.
        $this->workspaceId = (string) DB::table('silver.projects')
            ->where('project_id', $this->project->project_id)
            ->value('workspace_id');

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
