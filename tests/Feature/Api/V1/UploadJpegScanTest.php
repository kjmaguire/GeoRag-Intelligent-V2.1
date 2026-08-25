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
use Tests\TestCase;

/**
 * A JPEG in an exploration delivery is a SCANNED SHEET, and routes like one.
 *
 * RedStar shipped exactly one: `BMG_1990 Legend3.jpg`, the legend for a 1990
 * geological map — nothing but the unit descriptions that make the map
 * readable, and the single file in the delivery that no category accepted at
 * all. It is precisely ADR-0005's target.
 *
 * Nothing downstream needed changing, which is what makes this worth pinning
 * rather than trusting:
 *
 *   - `tiff_to_pdf` is Pillow's `Image.open` + `ImageSequence`, so a JPEG is
 *     a one-frame image and wraps unchanged. Verified on the real file:
 *     850,854 bytes in, a valid 1-page 144,439-byte PDF out.
 *   - a JPEG carries no CRS, and `_is_measurement_raster` returns False on
 *     its first line when the CRS is absent — so a scan always reaches OCR
 *     instead of being filed as a continuous-tone data grid.
 *
 * The routing itself is the fragile part, and it is fragile in a specific
 * way the controller's own docblock calls out: THREE places need the same
 * answer — the storage prefix, the dispatch target, and the category list.
 * Adding a format to two of the three files the upload under `reports/`
 * where the PDF sensor picks it up and fails on bytes that are not a PDF.
 * That is why the category list now spreads RASTER_REPORT_EXTS rather than
 * repeating it, and why these tests assert on the prefix and the workflow
 * separately rather than only on the 201.
 */
class UploadJpegScanTest extends TestCase
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
            'project_name' => 'JPEG Scan '.uniqid(),
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

    private function uploadUrl(): string
    {
        return "/api/v1/projects/{$this->project->project_id}/upload";
    }

    private function triggeredWorkflow(): ?string
    {
        foreach ($this->captured as $call) {
            if (preg_match('#/shadow/([a-z_]+)/trigger#', $call['url'], $m) === 1) {
                return $m[1];
            }
        }

        return null;
    }

    /** A tiny but structurally valid JPEG: SOI, APP0/JFIF, EOI. */
    private function jpeg(string $name = 'BMG_1990 Legend3.jpg'): UploadedFile
    {
        $bytes = "\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            .str_repeat("\x00", 64)."\xFF\xD9";

        return UploadedFile::fake()->createWithContent($name, $bytes);
    }

    public function test_jpeg_is_offered_under_reports(): void
    {
        $response = $this->actingAs($this->user)->getJson('/api/v1/upload/categories');
        $response->assertOk();

        /** @var array<string, list<string>> $categories */
        $categories = $response->json('categories');

        $this->assertContains('jpg', $categories['reports']);
        $this->assertContains('jpeg', $categories['reports']);
    }

    public function test_a_jpeg_upload_is_accepted(): void
    {
        // Before this it was a 422 at the door: no category listed the
        // extension, so a scanned legend could not be uploaded by any route.
        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), ['file' => $this->jpeg(), 'category' => 'reports'])
            ->assertCreated();
    }

    public function test_a_jpeg_dispatches_tiff_normalize_not_ingest_pdf(): void
    {
        // The wrong half of the fork. `ingest_pdf` would hand the PDF stack
        // bytes whose first four are FF D8 FF E0, and the run dies on a file
        // the geologist can plainly see is an image.
        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), ['file' => $this->jpeg(), 'category' => 'reports'])
            ->assertCreated();

        $this->assertSame('tiff_normalize', $this->triggeredWorkflow());
    }

    public function test_a_jpeg_is_stored_under_the_raster_prefix_not_reports(): void
    {
        // The third of the three places. Filed under `reports/` the PDF
        // sensor picks it up and fails on bytes that are not a PDF, which is
        // a different failure with the same "my file vanished" symptom.
        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), ['file' => $this->jpeg(), 'category' => 'reports'])
            ->assertCreated();

        $key = null;
        foreach ($this->captured as $call) {
            if (isset($call['data']['minio_key'])) {
                $key = (string) $call['data']['minio_key'];
                break;
            }
        }

        $this->assertNotNull($key, 'the dispatch payload must name the stored object');
        $this->assertStringStartsWith('tiff/', $key);
    }

    public function test_a_pdf_still_takes_the_direct_path(): void
    {
        // The fork has two sides and this change touched the one that
        // decides. A PDF must not start going through the raster wrap.
        $pdf = UploadedFile::fake()->createWithContent(
            'report.pdf',
            "%PDF-1.4\n%\xE2\xE3\xCF\xD3\n".str_repeat(' ', 64)."\n%%EOF\n",
        );

        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), ['file' => $pdf, 'category' => 'reports'])
            ->assertCreated();

        $this->assertSame('ingest_pdf', $this->triggeredWorkflow());
    }

    public function test_every_raster_extension_is_offered_by_the_category(): void
    {
        // The invariant the spread exists to hold: the dispatch list and the
        // accept list cannot disagree. A raster extension the API dispatches
        // but does not accept is a 422 on a format we believe we support.
        $response = $this->actingAs($this->user)->getJson('/api/v1/upload/categories');
        $response->assertOk();

        /** @var array<string, list<string>> $categories */
        $categories = $response->json('categories');

        foreach (['tif', 'tiff', 'rrd', 'jpg', 'jpeg'] as $ext) {
            $this->assertContains(
                $ext,
                $categories['reports'],
                "'.{$ext}' is dispatched as a raster but not accepted by the category",
            );
        }
    }
}
