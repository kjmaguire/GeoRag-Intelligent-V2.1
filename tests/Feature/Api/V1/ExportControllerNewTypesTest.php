<?php

namespace Tests\Feature\Api\V1;

use App\Jobs\GenerateExportJob;
use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Queue;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\TestCase;

/**
 * CC-02 Item 4 — accept-and-queue contract for the four new CSV
 * bulk export types (csv_samples, csv_assays, csv_lithology, csv_geochem).
 *
 * Each test asserts:
 *   1. The Form Request enum accepts the new export_type (200/202).
 *   2. The Export row lands with status='pending' and the right type.
 *   3. GenerateExportJob is queued with the correct export_id.
 *
 * The exporters themselves run against silver.* schema tables which
 * don't exist in the sqlite-in-memory test fixture; per-row CSV
 * generation correctness is verified manually against a live Postgres
 * instance, not in this suite.
 */
class ExportControllerNewTypesTest extends TestCase
{
    use RefreshDatabase;

    private Project $project;

    private User $user;

    protected function setUp(): void
    {
        parent::setUp();

        $this->project = Project::create([
            'project_name' => 'Export Test Project '.uniqid(),
            'crs_datum' => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);

        $this->user = User::factory()->create();
        $this->user->projects()->attach($this->project->project_id, ['role' => 'owner']);
        $this->actingAs($this->user);
    }

    /** @return iterable<string, array{string}> */
    public static function newExportTypeProvider(): iterable
    {
        yield 'samples' => ['csv_samples'];
        yield 'assays' => ['csv_assays'];
        yield 'lithology' => ['csv_lithology'];
        yield 'geochem' => ['csv_geochem'];
    }

    #[DataProvider('newExportTypeProvider')]
    public function test_can_create_export_of_new_type(string $exportType): void
    {
        Queue::fake();

        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/exports",
            ['export_type' => $exportType],
        );

        $response->assertStatus(202)
            ->assertJsonPath('data.export_type', $exportType)
            ->assertJsonPath('data.status', 'pending');

        $exportId = $response->json('data.export_id');
        $this->assertNotEmpty($exportId);

        Queue::assertPushed(GenerateExportJob::class, function (GenerateExportJob $job) use ($exportId) {
            $reflection = new \ReflectionProperty($job, 'exportId');
            $reflection->setAccessible(true);

            return $reflection->getValue($job) === $exportId;
        });
    }

    public function test_csv_lithology_accepts_min_confidence_filter(): void
    {
        Queue::fake();

        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/exports",
            [
                'export_type' => 'csv_lithology',
                'filters' => ['min_confidence' => 0.6],
            ],
        );

        $response->assertStatus(202)
            ->assertJsonPath('data.filters.min_confidence', 0.6);
    }

    public function test_csv_assays_accepts_element_and_qc_filters(): void
    {
        Queue::fake();

        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/exports",
            [
                'export_type' => 'csv_assays',
                'filters' => [
                    'element' => 'Au',
                    'exclude_rejected' => true,
                ],
            ],
        );

        $response->assertStatus(202)
            ->assertJsonPath('data.filters.element', 'Au')
            ->assertJsonPath('data.filters.exclude_rejected', true);
    }

    public function test_rejects_unknown_export_type(): void
    {
        Queue::fake();

        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/exports",
            ['export_type' => 'csv_unknown_bogus_type'],
        );

        $response->assertStatus(422)
            ->assertJsonValidationErrors(['export_type']);
        Queue::assertNothingPushed();
    }

    public function test_min_confidence_filter_rejects_out_of_range(): void
    {
        Queue::fake();

        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/exports",
            [
                'export_type' => 'csv_lithology',
                'filters' => ['min_confidence' => 1.5],
            ],
        );

        $response->assertStatus(422)
            ->assertJsonValidationErrors(['filters.min_confidence']);
    }
}
