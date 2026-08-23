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
 * `source_epsg` on UploadController::store — the operator's CRS override.
 *
 * The bug this closes, measured on real data: a shapefile delivered without
 * its .prj is parsed with source_crs defaulted to EPSG:4326, so a point at
 * (400797, 6117305) in UTM zone 4N lands in silver.spatial_features as
 * longitude 400,797 degrees. PostGIS does not range-check, the run reports
 * as 'partial', and Laravel treats 'partial' exactly like 'completed' — the
 * rows reach the map. The override is how a geologist says "this file is in
 * EPSG:26904" for a file that cannot say it itself.
 *
 * Three things are pinned here, in the order they can break:
 *
 *   1. The wire type. An EPSG *integer* bounded 1024-32767 — the rule
 *      StoreQueryRequest already fixed and the DB CHECK on
 *      silver.spatial_features.crs_epsg_native enforces. A 'EPSG:26904'
 *      string must be refused, or the platform ends up with two spellings
 *      of one concept.
 *   2. That it reaches the trigger. vendor_profile_id is the cautionary
 *      tale — validated on both upload surfaces since forever, forwarded
 *      only on the two PDF paths, silently absent from the spatial and
 *      tabular payloads that actually needed it. Asserting the payload the
 *      HTTP client sent is the only assertion that catches that.
 *   3. That it is withheld from ingest_well_logs, whose input model has no
 *      such field and no coordinates to place.
 *
 * SQLite-safe on purpose: store() writes no bronze.source_files row, so this
 * runs in the fast suite rather than needing the hand-ordered pgsql list.
 */
class UploadSourceEpsgTest extends TestCase
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
            'project_name' => 'Source EPSG Test '.uniqid(),
            'crs_datum' => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);

        // workspace_id is not mass-assignable and has no default, so
        // dispatchGeologyIngest() would take its "no workspace_id" skip
        // branch and never build a payload at all. Same fixture step as
        // UploadVendorProfileTest.
        DB::table('silver.projects')
            ->where('project_id', $this->project->project_id)
            ->update(['workspace_id' => (string) Str::uuid()]);

        $this->user = User::factory()->create();
        $this->user->projects()->attach($this->project->project_id, ['role' => 'owner']);

        config(['services.fastapi.service_key' => 'test-service-key-must-be-at-least-32-bytes-long']);

        Storage::fake('s3');

        $this->captured = [];
        Http::fake(function (ClientRequest $request) {
            $this->captured[] = [
                'url' => $request->url(),
                'data' => $request->data(),
            ];

            return Http::response(['workflow_run_id' => 'test-workflow-run-id'], 202);
        });
    }

    private function uploadUrl(): string
    {
        return "/api/v1/projects/{$this->project->project_id}/upload";
    }

    private function shapefileZip(): UploadedFile
    {
        // Content only has to survive UploadContentGuard: a real ZIP magic
        // header under a .zip name, and a well-formed (if tiny) archive so
        // rejectArchive() can open it.
        $path = tempnam(sys_get_temp_dir(), 'ge').'.zip';
        $zip = new \ZipArchive;
        $zip->open($path, \ZipArchive::CREATE | \ZipArchive::OVERWRITE);
        $zip->addFromString('faults.shp', str_repeat("\x00", 128));
        $zip->close();

        return new UploadedFile($path, 'faults.zip', 'application/zip', null, true);
    }

    private function csv(): UploadedFile
    {
        return UploadedFile::fake()->createWithContent(
            'collars.csv',
            "hole_id,east,north\nDH001,400797,6117305\n",
        );
    }

    /**
     * The payload posted to a given trigger endpoint, or null if it was
     * never called.
     *
     * @return array<string, mixed>|null
     */
    private function payloadFor(string $workflow): ?array
    {
        foreach ($this->captured as $call) {
            if (str_contains($call['url'], "/shadow/{$workflow}/trigger")) {
                return $call['data'];
            }
        }

        return null;
    }

    // ── Validation ───────────────────────────────────────────────────────

    public function test_source_epsg_is_optional(): void
    {
        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $this->shapefileZip(),
                'category' => 'spatial',
            ])
            ->assertCreated()
            ->assertJsonMissingPath('source_epsg');

        $payload = $this->payloadFor('ingest_spatial');
        $this->assertNotNull($payload, 'ingest_spatial was never triggered');
        $this->assertArrayNotHasKey(
            'source_epsg',
            $payload,
            'source_epsg must be omitted, not sent as null — a null says nothing '
                .'the omission does not, and the parser reads absence as "use the '
                .'CRS the file declares".',
        );
    }

    public function test_source_epsg_below_1024_is_rejected_with_the_platform_wording(): void
    {
        $response = $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $this->shapefileZip(),
                'category' => 'spatial',
                'source_epsg' => 1023,
            ]);

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['source_epsg']);

        $this->assertSame(
            'EPSG codes must be in the range 1024-32767.',
            $response->json('errors.source_epsg.0'),
            'store() validates inline, so the custom message must be passed to '
                .'validate() explicitly — otherwise this reads "The source epsg '
                .'field must be at least 1024." and contradicts StoreQueryRequest.',
        );
    }

    public function test_source_epsg_above_32767_is_rejected(): void
    {
        // The upper bound is the DB CHECK on crs_epsg_native, not an
        // arbitrary number: a code outside it fails the INSERT for every
        // feature in the file, at persist time, long after the 201.
        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $this->shapefileZip(),
                'category' => 'spatial',
                'source_epsg' => 32768,
            ])
            ->assertUnprocessable()
            ->assertJsonValidationErrors(['source_epsg']);
    }

    public function test_a_crs_string_is_refused(): void
    {
        // 'EPSG:26904' is how a human writes it and how
        // silver.spatial_features.source_crs stores it — and accepting it
        // here would put a second wire representation into a field whose
        // destination column is an integer.
        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $this->shapefileZip(),
                'category' => 'spatial',
                'source_epsg' => 'EPSG:26904',
            ])
            ->assertUnprocessable()
            ->assertJsonValidationErrors(['source_epsg']);
    }

    // ── Forwarding ───────────────────────────────────────────────────────

    public function test_source_epsg_reaches_the_ingest_spatial_trigger(): void
    {
        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $this->shapefileZip(),
                'category' => 'spatial',
                'source_epsg' => 26904,
            ])
            ->assertCreated()
            ->assertJsonPath('source_epsg', 26904);

        $payload = $this->payloadFor('ingest_spatial');
        $this->assertNotNull($payload, 'ingest_spatial was never triggered');
        $this->assertSame(26904, $payload['source_epsg'] ?? null);
    }

    public function test_source_epsg_reaches_the_ingest_tabular_trigger(): void
    {
        // The half that vendor_profile_id never got. Without this the drill
        // CSV path keeps assuming DEFAULT_SOURCE_EPSG=32613 forever.
        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $this->csv(),
                'category' => 'collars',
                'source_epsg' => 26904,
            ])
            ->assertCreated();

        $payload = $this->payloadFor('ingest_tabular');
        $this->assertNotNull($payload, 'ingest_tabular was never triggered');
        $this->assertSame(26904, $payload['source_epsg'] ?? null);
        // The category is still the sheet-type hint; adding one field must
        // not displace the other.
        $this->assertSame('collar', $payload['sheet_type'] ?? null);
    }

    public function test_source_epsg_is_withheld_from_ingest_well_logs(): void
    {
        // IngestWellLogsInput declares no source_epsg. A LAS file's curves
        // are depths, not coordinates; sending the key would at best be
        // ignored and at worst rejected at the pydantic boundary.
        $las = UploadedFile::fake()->createWithContent(
            'hole.las',
            "~Version\nVERS. 2.0 :\n~Well\nSTRT.M 0.0 :\n~ASCII\n0.0 1.0\n",
        );

        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $las,
                'category' => 'well_logs',
                'source_epsg' => 26904,
            ])
            ->assertCreated();

        $payload = $this->payloadFor('ingest_well_logs');
        $this->assertNotNull($payload, 'ingest_well_logs was never triggered');
        $this->assertArrayNotHasKey('source_epsg', $payload);
    }
}
