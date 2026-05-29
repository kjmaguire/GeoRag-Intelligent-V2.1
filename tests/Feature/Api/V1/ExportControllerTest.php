<?php

namespace Tests\Feature\Api\V1;

use App\Jobs\GenerateExportJob;
use App\Models\Collar;
use App\Models\Export;
use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Queue;
use Tests\TestCase;

/**
 * Feature tests for ExportController.
 *
 * Runs under the suite's sqlite-in-memory fixture (see tests/bootstrap.php and
 * TestCase::refreshApplication for the PG→sqlite DDL rewrites). RefreshDatabase
 * rebuilds the schema per test, matching every other V1 controller test.
 *
 * Queue::fake() is used throughout so that GenerateExportJob is never actually
 * dispatched — we assert that it is queued with the correct export_id, not that
 * the file generation itself works (that is covered by service-level unit tests).
 */
class ExportControllerTest extends TestCase
{
    use RefreshDatabase;

    private Project $project;
    private User $user;

    protected function setUp(): void
    {
        parent::setUp();

        $this->project = Project::create([
            'project_name'          => 'Export Test Project ' . uniqid(),
            'crs_datum'             => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);

        $this->user = User::factory()->create();
        $this->user->projects()->attach($this->project->project_id, ['role' => 'owner']);
        $this->actingAs($this->user);
    }

    // -------------------------------------------------------------------------
    // store — CSV collars
    // -------------------------------------------------------------------------

    public function test_can_create_csv_collars_export(): void
    {
        Queue::fake();

        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/exports",
            ['export_type' => 'csv_collars']
        );

        $response->assertStatus(202)
            ->assertJsonPath('data.export_type', 'csv_collars')
            ->assertJsonPath('data.status', 'pending')
            ->assertJsonPath('data.project_id', $this->project->project_id)
            ->assertJsonStructure([
                'data'        => ['export_id', 'export_type', 'status', 'project_id', 'created_at'],
                'status_url',
                'message',
            ]);

        $exportId = $response->json('data.export_id');
        $this->assertNotEmpty($exportId);

        Queue::assertPushed(GenerateExportJob::class, function (GenerateExportJob $job) use ($exportId) {
            // Verify the job carries the right export_id by reflecting the property.
            $reflection = new \ReflectionProperty($job, 'exportId');
            $reflection->setAccessible(true);

            return $reflection->getValue($job) === $exportId;
        });
    }

    // -------------------------------------------------------------------------
    // store — CSA bundle
    // -------------------------------------------------------------------------

    public function test_can_create_csa_bundle(): void
    {
        Queue::fake();

        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/exports",
            ['export_type' => 'csa_bundle']
        );

        $response->assertStatus(202)
            ->assertJsonPath('data.export_type', 'csa_bundle')
            ->assertJsonPath('data.status', 'pending');

        Queue::assertPushed(GenerateExportJob::class);
    }

    // -------------------------------------------------------------------------
    // store — with filters
    // -------------------------------------------------------------------------

    public function test_can_create_export_with_filters(): void
    {
        Queue::fake();

        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/exports",
            [
                'export_type' => 'csv_collars',
                'filters'     => [
                    'hole_type'  => 'Diamond',
                    'min_depth'  => 100.0,
                    'max_depth'  => 500.0,
                ],
            ]
        );

        $response->assertStatus(202);

        $export = Export::find($response->json('data.export_id'));
        $this->assertNotNull($export);
        $this->assertSame('Diamond', $export->filters['hole_type']);
        $this->assertEquals(100.0, $export->filters['min_depth']);
    }

    // -------------------------------------------------------------------------
    // store — validation failures
    // -------------------------------------------------------------------------

    public function test_store_returns_422_for_invalid_export_type(): void
    {
        Queue::fake();

        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/exports",
            ['export_type' => 'invalid_type']
        );

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['export_type']);

        Queue::assertNothingPushed();
    }

    public function test_store_returns_422_when_export_type_missing(): void
    {
        Queue::fake();

        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/exports",
            []
        );

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['export_type']);
    }

    public function test_store_returns_403_for_non_member_project(): void
    {
        Queue::fake();

        // Non-member requests MUST get 403, not 404 — that way the API cannot
        // be used to enumerate which project UUIDs exist.
        $response = $this->postJson(
            '/api/v1/projects/00000000-0000-0000-0000-000000000000/exports',
            ['export_type' => 'csv_collars']
        );

        $response->assertForbidden();
        Queue::assertNothingPushed();
    }

    // -------------------------------------------------------------------------
    // show — status polling
    // -------------------------------------------------------------------------

    public function test_can_fetch_export_status(): void
    {
        $export = Export::create([
            'project_id'  => $this->project->project_id,
            'export_type' => 'csv_collars',
            'status'      => 'pending',
            'filters'     => [],
        ]);

        $response = $this->getJson(
            "/api/v1/projects/{$this->project->project_id}/exports/{$export->export_id}"
        );

        $response->assertOk()
            ->assertJsonPath('data.export_id', $export->export_id)
            ->assertJsonPath('data.status', 'pending')
            ->assertJsonPath('data.export_type', 'csv_collars');
    }

    public function test_show_returns_download_url_when_completed(): void
    {
        $export = Export::create([
            'project_id'             => $this->project->project_id,
            'export_type'            => 'csv_collars',
            'status'                 => 'completed',
            'filters'                => [],
            'minio_path'             => 'georag-exports/test/collars.csv',
            'download_url'           => 'https://minio.example.com/signed-url',
            'download_url_expires_at' => now()->addHours(23),
            'completed_at'           => now(),
            'file_count'             => 1,
            'total_size_bytes'       => 1024,
        ]);

        $response = $this->getJson(
            "/api/v1/projects/{$this->project->project_id}/exports/{$export->export_id}"
        );

        $response->assertOk()
            ->assertJsonPath('data.status', 'completed')
            ->assertJsonPath('data.download_url', 'https://minio.example.com/signed-url');
    }

    public function test_show_returns_404_for_export_in_wrong_project(): void
    {
        $otherProject = Project::create([
            'project_name'          => 'Other Project ' . uniqid(),
            'crs_datum'             => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);

        $export = Export::create([
            'project_id'  => $otherProject->project_id,
            'export_type' => 'csv_collars',
            'status'      => 'pending',
            'filters'     => [],
        ]);

        $response = $this->getJson(
            "/api/v1/projects/{$this->project->project_id}/exports/{$export->export_id}"
        );

        $response->assertNotFound();
    }

    // -------------------------------------------------------------------------
    // index — list exports
    // -------------------------------------------------------------------------

    public function test_index_returns_exports_for_project(): void
    {
        Export::create([
            'project_id'  => $this->project->project_id,
            'export_type' => 'csv_collars',
            'status'      => 'pending',
            'filters'     => [],
        ]);

        Export::create([
            'project_id'  => $this->project->project_id,
            'export_type' => 'csa_bundle',
            'status'      => 'completed',
            'filters'     => [],
        ]);

        $response = $this->getJson(
            "/api/v1/projects/{$this->project->project_id}/exports"
        );

        $response->assertOk();
        $this->assertGreaterThanOrEqual(2, count($response->json('data')));
    }

    public function test_index_returns_403_for_non_member_project(): void
    {
        // Non-member requests MUST get 403, not 404 — that way the API cannot
        // be used to enumerate which project UUIDs exist.
        $response = $this->getJson(
            '/api/v1/projects/00000000-0000-0000-0000-000000000000/exports'
        );

        $response->assertForbidden();
    }

    // -------------------------------------------------------------------------
    // download — 409 when not completed
    // -------------------------------------------------------------------------

    public function test_download_returns_409_when_export_not_completed(): void
    {
        $export = Export::create([
            'project_id'  => $this->project->project_id,
            'export_type' => 'csv_collars',
            'status'      => 'pending',
            'filters'     => [],
        ]);

        $response = $this->getJson("/api/v1/exports/{$export->export_id}/download");

        $response->assertStatus(409);
    }

    public function test_download_returns_404_for_nonexistent_export(): void
    {
        $response = $this->getJson(
            '/api/v1/exports/00000000-0000-0000-0000-000000000000/download'
        );

        $response->assertNotFound();
    }
}
