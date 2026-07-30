<?php

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\UploadedFile;
use Illuminate\Support\Facades\Storage;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\TestCase;

/**
 * B2 (2026-07-28) — categories retired with the Dagster services.
 *
 * Before this change an upload of, say, a collars CSV returned 201, wrote the
 * object and a bronze manifest row, and then nothing happened: only 'reports'
 * and 'archive' dispatch a Hatchet workflow, and the Dagster
 * minio_upload_sensor that would have picked the rest up was verified STOPPED
 * on the live stack. The user got a success response for work that was
 * silently dropped.
 *
 * These tests pin the replacement contract: retired categories are refused
 * with an explanatory 422, live categories still work, and the discovery
 * endpoint reports both sets so a client can render the picker honestly.
 */
class UploadRetiredCategoryTest extends TestCase
{
    use RefreshDatabase;

    private User $user;

    private Project $project;

    protected function setUp(): void
    {
        parent::setUp();

        $this->user = User::factory()->create();
        $this->project = Project::factory()->create();
        $this->user->projects()->attach($this->project->project_id, ['role' => 'owner']);
    }

    private function uploadUrl(): string
    {
        return "/api/v1/projects/{$this->project->project_id}/upload";
    }

    /**
     * @return list<array{0: string, 1: string}>
     */
    public static function retiredCategoryProvider(): array
    {
        return [
            'drillhole collars' => ['collars', 'collars.csv'],
            'downhole surveys' => ['surveys', 'surveys.csv'],
            'lithology intervals' => ['lithology', 'litho.csv'],
            'assay samples' => ['samples', 'samples.csv'],
            'well logs' => ['well_logs', 'hole.las'],
            'spatial layers' => ['spatial', 'claims.geojson'],
            'excel workbooks' => ['excel', 'book.xlsx'],
            'seismic volumes' => ['seismic', 'line.sgy'],
            'xyz grids' => ['xyz', 'grid.xyz'],
            'geophysics summaries' => ['geophysics', 'survey.json'],
        ];
    }

    #[DataProvider('retiredCategoryProvider')]
    public function test_retired_category_is_refused_with_an_explanation(
        string $category,
        string $filename,
    ): void {
        Storage::fake('s3');
        $this->actingAs($this->user, 'sanctum');

        $response = $this->postJson($this->uploadUrl(), [
            'file' => UploadedFile::fake()->create($filename, 4),
            'category' => $category,
        ]);

        $response->assertStatus(422);
        $response->assertJsonPath('retired_category', $category);
        // The message must say WHY, not just "invalid" — a caller hitting this
        // is most likely an older client that used to get a 201.
        $this->assertStringContainsString(
            'no longer accepted',
            $response->json('message'),
        );

        // Nothing may be written for a refused upload.
        $this->assertEmpty(
            Storage::disk('s3')->allFiles(),
            'A retired-category upload must not write to object storage',
        );
    }

    public function test_live_report_category_still_accepts_uploads(): void
    {
        Storage::fake('s3');
        $this->actingAs($this->user, 'sanctum');

        $response = $this->postJson($this->uploadUrl(), [
            'file' => UploadedFile::fake()->createWithContent(
                'report.pdf',
                "%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n",
            ),
            'category' => 'reports',
        ]);

        $response->assertCreated();
    }

    public function test_categories_endpoint_separates_live_from_retired(): void
    {
        $this->actingAs($this->user, 'sanctum');

        $response = $this->getJson('/api/v1/upload/categories');
        $response->assertOk();

        $live = $response->json('categories');
        $retired = $response->json('retired');

        $this->assertIsArray($live);
        $this->assertIsArray($retired);

        // Only the two Hatchet-dispatching categories remain live.
        $this->assertSame(['reports', 'archive'], array_keys($live));

        // A category must never appear in both — that would let a client offer
        // an upload the API refuses.
        $this->assertEmpty(
            array_intersect(array_keys($live), array_keys($retired)),
            'A category cannot be both live and retired',
        );

        $this->assertArrayHasKey('collars', $retired);
    }
}
