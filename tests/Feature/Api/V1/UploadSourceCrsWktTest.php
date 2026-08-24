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
 * `source_crs_wkt` on UploadController::store — the donated `.prj` as text.
 *
 * The wizard's CRS donation copies a `.prj` into every shapefile bundle that
 * declares none of its own. A `.dxf` (or `.dgn`) cannot take the copy — it
 * travels as one file, not a ZIP — so for those the donation rides the upload
 * as this field and ingest_spatial resolves it to an EPSG integer with
 * pyproj. Measured on the RedStar delivery: the same GeoPoints_2005.prj that
 * rescued seven shapefiles named the CRS the lone DXF needed, and nothing
 * could carry it there.
 *
 * Pinned here, mirroring UploadSourceEpsgTest:
 *
 *   1. That it reaches the ingest_spatial trigger — forwarding is the half
 *      that silently rots (vendor_profile_id is the cautionary tale).
 *   2. That a typed source_epsg suppresses it: the workflow prefers the
 *      typed code regardless, so sending both is dead weight.
 *   3. That it is withheld from ingest_tabular, whose input model has no
 *      such field.
 */
class UploadSourceCrsWktTest extends TestCase
{
    use RefreshDatabase;

    /** ESRI-style, no AUTHORITY clause — exactly what real donors look like. */
    private const REDSTAR_PRJ = 'PROJCS["NAD_1983_UTM_Zone_4N",'
        .'GEOGCS["GCS_North_American_1983",DATUM["D_North_American_1983",'
        .'SPHEROID["GRS_1980",6378137.0,298.257222101]],'
        .'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
        .'PROJECTION["Transverse_Mercator"],'
        .'PARAMETER["False_Easting",500000.0],'
        .'PARAMETER["False_Northing",0.0],'
        .'PARAMETER["Central_Meridian",-159.0],'
        .'PARAMETER["Scale_Factor",0.9996],'
        .'PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]';

    private User $user;

    private Project $project;

    /** @var list<array{url: string, data: array<string, mixed>}> */
    private array $captured = [];

    protected function setUp(): void
    {
        parent::setUp();

        $this->project = Project::create([
            'project_name' => 'Source CRS WKT Test '.uniqid(),
            'crs_datum' => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);

        // Same fixture step as UploadSourceEpsgTest: without a workspace_id,
        // dispatchGeologyIngest() takes its skip branch and never builds a
        // payload at all.
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

    private function dxf(): UploadedFile
    {
        // DXF has no reliable magic signature, so UploadContentGuard is
        // deliberately lenient about it — plain text survives the sniff.
        return UploadedFile::fake()->createWithContent(
            'plan.dxf',
            "0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n",
        );
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

    public function test_source_crs_wkt_is_optional_and_omitted_not_nulled(): void
    {
        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $this->dxf(),
                'category' => 'spatial',
            ])
            ->assertCreated();

        $payload = $this->payloadFor('ingest_spatial');
        $this->assertNotNull($payload, 'ingest_spatial was never triggered');
        $this->assertArrayNotHasKey('source_crs_wkt', $payload);
    }

    public function test_an_oversized_wkt_is_refused_at_validation(): void
    {
        // The input model caps the field at 65536 too; refusing here keeps
        // the failure at the 422 boundary instead of a pydantic error on the
        // trigger an upload later.
        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $this->dxf(),
                'category' => 'spatial',
                'source_crs_wkt' => str_repeat('x', 70000),
            ])
            ->assertUnprocessable()
            ->assertJsonValidationErrors(['source_crs_wkt']);
    }

    // ── Forwarding ───────────────────────────────────────────────────────

    public function test_the_wkt_reaches_the_ingest_spatial_trigger(): void
    {
        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $this->dxf(),
                'category' => 'spatial',
                'source_crs_wkt' => self::REDSTAR_PRJ,
            ])
            ->assertCreated();

        $payload = $this->payloadFor('ingest_spatial');
        $this->assertNotNull($payload, 'ingest_spatial was never triggered');
        $this->assertSame(self::REDSTAR_PRJ, $payload['source_crs_wkt'] ?? null);
    }

    public function test_a_typed_epsg_suppresses_the_wkt(): void
    {
        // The workflow ignores the WKT whenever source_epsg is present — a
        // typed code outranks a found copy — so the controller does not send
        // a field that can only be dead weight.
        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $this->dxf(),
                'category' => 'spatial',
                'source_epsg' => 26904,
                'source_crs_wkt' => self::REDSTAR_PRJ,
            ])
            ->assertCreated();

        $payload = $this->payloadFor('ingest_spatial');
        $this->assertNotNull($payload, 'ingest_spatial was never triggered');
        $this->assertSame(26904, $payload['source_epsg'] ?? null);
        $this->assertArrayNotHasKey('source_crs_wkt', $payload);
    }

    public function test_the_wkt_is_withheld_for_non_cad_spatial_files(): void
    {
        // Only .dxf/.dgn have no CRS concept and no sidecar GDAL reads —
        // the reason the donation must travel as text at all. For any
        // other spatial format a forwarded WKT can only mislead: the
        // parser ignores it for a CRS-declaring file while the workflow
        // had already trusted it. The wizard never sends it for those;
        // this pins the same rule against a raw API caller.
        $path = tempnam(sys_get_temp_dir(), 'ge').'.zip';
        $zip = new \ZipArchive;
        $zip->open($path, \ZipArchive::CREATE | \ZipArchive::OVERWRITE);
        $zip->addFromString('faults.shp', str_repeat("\x00", 128));
        $zip->close();
        $bundle = new UploadedFile($path, 'faults.zip', 'application/zip', null, true);

        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $bundle,
                'category' => 'spatial',
                'source_crs_wkt' => self::REDSTAR_PRJ,
            ])
            ->assertCreated();

        $payload = $this->payloadFor('ingest_spatial');
        $this->assertNotNull($payload, 'ingest_spatial was never triggered');
        $this->assertArrayNotHasKey('source_crs_wkt', $payload);
    }

    public function test_the_wkt_is_withheld_from_ingest_tabular(): void
    {
        // IngestTabularInput declares no source_crs_wkt: nothing in a CSV
        // declares a projection, and the tabular path takes its override as
        // an integer only. Sending the key would be rejected at the pydantic
        // boundary.
        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $this->csv(),
                'category' => 'collars',
                'source_crs_wkt' => self::REDSTAR_PRJ,
            ])
            ->assertCreated();

        $payload = $this->payloadFor('ingest_tabular');
        $this->assertNotNull($payload, 'ingest_tabular was never triggered');
        $this->assertArrayNotHasKey('source_crs_wkt', $payload);
    }
}
