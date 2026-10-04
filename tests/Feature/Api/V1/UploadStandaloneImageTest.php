<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\Client\Request as ClientRequest;
use Illuminate\Http\UploadedFile;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\Str;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\TestCase;

/**
 * PNG, BMP, GIF and WebP are scanned-image uploads, routed exactly like JPEG.
 *
 * They are accepted under `reports`, stored under the `tiff/` prefix and
 * dispatched to `tiff_normalize` (the Pillow -> PDF -> ingest_pdf wrap), never
 * to `ingest_pdf` directly.
 */
class UploadStandaloneImageTest extends TestCase
{
    use RefreshDatabase;

    private User $user;

    private Project $project;

    /** @var list<array{url: string, data: array<string, mixed>}> */
    private array $captured = [];

    protected function setUp(): void
    {
        parent::setUp();

        $this->project = Project::create([
            'project_name' => 'Image Scan '.uniqid(),
            'crs_datum' => 'EPSG:26904',
            'orientation_reference' => 'BOH',
        ]);
        DB::table('silver.projects')
            ->where('project_id', $this->project->project_id)
            ->update(['workspace_id' => (string) Str::uuid()]);

        $this->user = User::factory()->create();
        $this->user->projects()->attach($this->project->project_id, ['role' => 'owner']);

        config(['services.fastapi.service_key' => 'test-service-key-must-be-at-least-32-bytes-long']);

        Storage::fake('s3');

        $this->captured = [];
        Http::fake(function (ClientRequest $request) {
            $this->captured[] = ['url' => $request->url(), 'data' => $request->data()];

            return Http::response(['workflow_run_id' => 'test-workflow-run-id'], 202);
        });
    }

    /**
     * @return array<string, array{string, string}>
     */
    public static function imageFormats(): array
    {
        return [
            'png' => ['scan.png', "\x89PNG\r\n\x1a\n".str_repeat("\x00", 64)],
            'bmp' => ['scan.bmp', 'BM'.str_repeat("\x00", 64)],
            'gif' => ['scan.gif', 'GIF89a'.str_repeat("\x00", 64)],
            'webp' => ['scan.webp', 'RIFF'."\x00\x00\x00\x00".'WEBP'.str_repeat("\x00", 64)],
        ];
    }

    private function uploadUrl(): string
    {
        return "/api/v1/projects/{$this->project->project_id}/upload";
    }

    public function test_the_new_formats_are_offered_under_reports(): void
    {
        $response = $this->actingAs($this->user)->getJson('/api/v1/upload/categories');
        $response->assertOk();

        /** @var array<string, list<string>> $categories */
        $categories = $response->json('categories');

        foreach (['png', 'bmp', 'gif', 'webp'] as $ext) {
            $this->assertContains($ext, $categories['reports'], "'.{$ext}' is not offered");
        }
    }

    #[DataProvider('imageFormats')]
    public function test_an_image_is_accepted_stored_under_tiff_and_sent_to_tiff_normalize(
        string $name,
        string $bytes,
    ): void {
        $file = UploadedFile::fake()->createWithContent($name, $bytes);

        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), ['file' => $file, 'category' => 'reports'])
            ->assertCreated();

        $workflow = null;
        $key = null;
        foreach ($this->captured as $call) {
            if ($workflow === null && preg_match('#/shadow/([a-z_]+)/trigger#', $call['url'], $m) === 1) {
                $workflow = $m[1];
            }
            if ($key === null && isset($call['data']['minio_key'])) {
                $key = (string) $call['data']['minio_key'];
            }
        }

        $this->assertSame('tiff_normalize', $workflow);
        $this->assertNotNull($key);
        $this->assertStringStartsWith('tiff/', $key);
    }
}
