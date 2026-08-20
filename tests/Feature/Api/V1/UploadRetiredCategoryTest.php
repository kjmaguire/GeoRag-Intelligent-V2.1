<?php

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\UploadedFile;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\Str;
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

        // workspace_id isn't factory-set (and isn't mass-assignable) — the
        // 'reports' category's dispatchShadowIfPdf() needs a real one to
        // proceed past its "no workspace_id" skip branch and actually
        // attempt ingestion dispatch (which now correctly 502s if it can't
        // — see UploadController's ingestion_dispatch_failed fix). Only
        // test_live_report_category_still_accepts_uploads exercises
        // 'reports'; the retired-category tests never reach dispatch at
        // all (422 before that point), so this setup is harmless for them.
        DB::table('silver.projects')
            ->where('project_id', $this->project->project_id)
            ->update(['workspace_id' => (string) Str::uuid()]);
        config(['services.fastapi.service_key' => 'test-service-key-must-be-at-least-32-bytes-long']);
        Http::fake([
            '*/internal/v1/shadow/ingest_pdf/trigger' => Http::response(
                ['hatchet_workflow_run_id' => 'test-run-id'],
                202,
            ),
        ]);
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
        // 2026-08-20: collars / surveys / lithology / samples / excel /
        // spatial were restored — ingest_tabular and ingest_spatial give them
        // live Hatchet consumers, so they now belong in the live-category
        // test below rather than here. What remains is what is still
        // genuinely consumer-less.
        return [
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

        // Every live category must dispatch to a workflow that exists. This
        // is the invariant that matters: a category listed as live with no
        // consumer returns 201, writes the object, and is never processed —
        // the exact failure the retired list was created to stop.
        foreach (['reports', 'archive', 'collars', 'surveys', 'lithology',
            'samples', 'excel', 'spatial', 'well_logs'] as $expected) {
            $this->assertArrayHasKey(
                $expected, $live, "'{$expected}' should be a live category",
            );
        }

        // A category must never appear in both — that would let a client offer
        // an upload the API refuses.
        $this->assertEmpty(
            array_intersect(array_keys($live), array_keys($retired)),
            'A category cannot be both live and retired',
        );

        // Still genuinely consumer-less: parsers exist for SEGY and XYZ but
        // no workflow calls them yet.
        $this->assertArrayHasKey('seismic', $retired);
        $this->assertArrayNotHasKey('collars', $retired);
        $this->assertArrayNotHasKey('well_logs', $retired);
    }

    public function test_restored_geology_categories_accept_their_formats(): void
    {
        Storage::fake('s3');
        $this->actingAs($this->user, 'sanctum');

        // The upload must be ACCEPTED (not 422'd as retired). Dispatch itself
        // reaches FastAPI, which is not running under test — a 502
        // 'ingestion_dispatch_failed' is therefore a pass here, because it
        // proves the request got past category validation and into dispatch.
        foreach ([
            ['collars', 'collars.csv'],
            ['excel', 'book.xlsx'],
            ['spatial', 'claims.geojson'],
            ['spatial', 'project.qgz'],
            ['well_logs', 'EL-001.las'],
        ] as [$category, $filename]) {
            $response = $this->postJson($this->uploadUrl(), [
                'file' => UploadedFile::fake()->create($filename, 4),
                'category' => $category,
            ]);

            $this->assertNotSame(
                422, $response->status(),
                "'{$category}' + {$filename} was refused; it should be accepted",
            );
            $this->assertNull(
                $response->json('retired_category'),
                "'{$category}' should no longer report as retired",
            );
        }
    }
}
