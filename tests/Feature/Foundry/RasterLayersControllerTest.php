<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Http\Controllers\Foundry\RasterLayersController;
use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Route;
use Illuminate\Support\Str;
use Inertia\Testing\AssertableInertia;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * RasterLayersController — the project raster catalogue.
 *
 * Postgres-only, and not merely for convenience: every assertion here goes
 * through PostGIS (ST_MakeEnvelope on the way in, ST_AsGeoJSON / ST_XMin on
 * the way out) and `silver.raster_layers` exists only in the pgsql test DB.
 * Run with:
 *   php artisan test -c phpunit.pgsql.xml --filter=RasterLayersControllerTest
 *
 * On tenant isolation, stated plainly so nobody reads more into
 * {@see test_a_raster_in_another_workspace_is_not_listed} than it proves:
 * RLS on silver.raster_layers is ENABLED but not FORCED, and this suite
 * connects as the table owner, so the policy is bypassed here exactly as it
 * would be for any owning role. What the test therefore proves is the
 * controller's own explicit `workspace_id = ?` predicate — which is the
 * point, because that predicate is what keeps the read fail-closed when the
 * policy does not apply. The GUC path is covered separately by the tenancy
 * suite.
 */
final class RasterLayersControllerTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private User $user;

    private Project $project;

    private string $workspaceId;

    protected function setUp(): void
    {
        parent::setUp();

        $this->user = User::factory()->create();
        $this->workspaceId = $this->makeWorkspace('Raster Workspace');

        $this->project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $this->project->project_id],
        );
        $this->user->projects()->syncWithoutDetaching([
            $this->project->project_id => ['role' => 'owner'],
        ]);

        $this->registerRouteUntilWired();
    }

    /**
     * Bootstrap shim, inert the moment the real route lands.
     *
     * The controller and this test were written in one unit; the route in
     * routes/web.php is a separate edit to a shared file. Until that edit
     * lands every case here would 404 and prove nothing about the SQL it
     * exists to pin, so the route is declared locally — but ONLY when the
     * application has not declared it. Once routes/web.php names
     * `foundry.rasters` this method returns immediately and the suite
     * exercises the real wiring, middleware and all.
     *
     * {@see test_the_page_is_wired_into_routes_web_php()} is what stops the
     * shim standing in for the wiring forever: it fails for as long as the
     * real route is missing.
     */
    private function registerRouteUntilWired(): void
    {
        if (Route::has('foundry.rasters')) {
            return;
        }

        Route::middleware(['web', 'auth:sanctum'])
            ->get('/projects/{slug}/rasters', [RasterLayersController::class, 'index'])
            ->where('slug', '[a-z0-9\-]+')
            ->name('foundry.rasters');
    }

    public function test_the_page_is_wired_into_routes_web_php(): void
    {
        // Deliberately reads the file rather than Route::has(): setUp's shim
        // satisfies Route::has() by itself, so asking the router would let
        // the shim answer for the wiring it is standing in for.
        $web = (string) file_get_contents(base_path('routes/web.php'));

        $this->assertStringContainsString(
            'RasterLayersController',
            $web,
            'routes/web.php does not route anything to RasterLayersController, so '
            .'/projects/{slug}/rasters 404s in the real application and '
            .'silver.raster_layers still has no reader. Add:'."\n\n"
            .'    Route::get(\'/projects/{slug}/rasters\', '
            .'[RasterLayersController::class, \'index\'])'."\n"
            .'        ->where(\'slug\', \'[a-z0-9\-]+\')'."\n"
            .'        ->name(\'foundry.rasters\');',
        );
    }

    private function makeWorkspace(string $name): string
    {
        $id = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$id, $name, 'ws-'.substr($id, 0, 8)],
        );

        return $id;
    }

    /**
     * Insert one raster the way `raster_metadata.py` does — same columns,
     * same ST_MakeEnvelope for the bbox, so a shape change in the writer
     * shows up here rather than in production.
     *
     * @param array<string, mixed> $overrides
     */
    private function insertRaster(array $overrides = []): string
    {
        $rasterId = (string) Str::uuid();

        $row = array_merge([
            'layer_name' => 'Geologic_Map_Unga_1982b',
            'source_file' => 'uploads/x/20260820_155245_Geologic_Map_Unga_1982b_utm.tif',
            'sha' => str_repeat('a', 64),
            'format' => 'GTiff',
            'driver' => 'GTiff',
            'width' => 4096,
            'height' => 3072,
            'band_count' => 3,
            'crs' => 'EPSG:32605',
            'crs_confidence' => 0.9,
            'pixel_size_x' => 2.5,
            'pixel_size_y' => -2.5,
            'is_cog' => false,
            'has_alpha' => false,
            'band_stats' => [
                ['band_index' => 1, 'dtype' => 'uint8', 'min' => 0, 'max' => 255, 'mean' => 121.5, 'nodata' => null, 'description' => 'red'],
            ],
            'warnings' => [],
            'bounds' => [-160.5, 55.1, -160.0, 55.4],
            'project_id' => $this->project->project_id,
            'workspace_id' => $this->workspaceId,
        ], $overrides);

        DB::statement(
            <<<'SQL'
                INSERT INTO silver.raster_layers (
                    raster_id, project_id, workspace_id, layer_name, source_file,
                    source_file_sha256, format, driver, width, height, band_count,
                    crs, crs_confidence, pixel_size_x, pixel_size_y,
                    is_cog, has_alpha, band_stats, warnings, bbox
                ) VALUES (
                    ?::uuid, ?::uuid, ?::uuid, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?::jsonb, ?::jsonb,
                    CASE WHEN ?::double precision IS NULL THEN NULL
                         ELSE ST_MakeEnvelope(?, ?, ?, ?, 4326) END
                )
            SQL,
            [
                $rasterId,
                $row['project_id'],
                $row['workspace_id'],
                $row['layer_name'],
                $row['source_file'],
                $row['sha'],
                $row['format'],
                $row['driver'],
                $row['width'],
                $row['height'],
                $row['band_count'],
                $row['crs'],
                $row['crs_confidence'],
                $row['pixel_size_x'],
                $row['pixel_size_y'],
                $row['is_cog'],
                $row['has_alpha'],
                json_encode($row['band_stats']),
                json_encode($row['warnings']),
                $row['bounds'][0] ?? null,
                $row['bounds'][0] ?? null,
                $row['bounds'][1] ?? null,
                $row['bounds'][2] ?? null,
                $row['bounds'][3] ?? null,
            ],
        );

        return $rasterId;
    }

    /**
     * A `silver.reports` row as `ingest_pdf` writes it for a TIFF that went
     * through the `tiff_normalize` PDF wrap.
     */
    private function insertDerivedTiffReport(string $sha8, string $stem): string
    {
        $reportId = (string) Str::uuid();
        DB::table('silver.reports')->insert([
            'report_id' => $reportId,
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->project->project_id,
            'title' => $stem,
            'parser_used' => 'fitz',
            'is_scanned' => true,
            'version' => 1,
            'qp_name' => '{}',
            'source_object_key' => "reports/{$this->project->project_id}/tiff-derived-{$sha8}-{$stem}.pdf",
        ]);

        return $reportId;
    }

    public function test_a_non_member_gets_404(): void
    {
        $outsider = User::factory()->create();

        $this->actingAs($outsider)
            ->get("/projects/{$this->project->slug}/rasters")
            ->assertStatus(404);
    }

    public function test_the_page_lists_the_projects_rasters_with_their_header_facts(): void
    {
        $this->insertRaster();

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/rasters")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->component('Foundry/RasterLayers')
                    ->where('project.slug', $this->project->slug)
                    ->has('rasters', 1)
                    ->where('rasters.0.layer_name', 'Geologic_Map_Unga_1982b')
                    // The upload-key timestamp prefix is stripped, so the
                    // reader sees the file as they named it.
                    ->where('rasters.0.source_filename', 'Geologic_Map_Unga_1982b_utm.tif')
                    ->where('rasters.0.width', 4096)
                    ->where('rasters.0.height', 3072)
                    ->where('rasters.0.band_count', 3)
                    ->where('rasters.0.crs', 'EPSG:32605')
                    ->where('rasters.0.crs_confidence', 0.9)
                    ->where('rasters.0.is_cog', false)
                    ->where('rasters.0.georeferenced', true)
                    ->where('summary.total', 1)
                    ->where('summary.georeferenced', 1)
                    ->where('summary.missing_crs', 0),
            );
    }

    public function test_the_bbox_comes_back_as_drawable_geojson_and_bounds(): void
    {
        $this->insertRaster(['bounds' => [-160.5, 55.1, -160.0, 55.4]]);

        $props = $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/rasters")
            ->assertOk()
            ->viewData('page')['props'];

        $raster = $props['rasters'][0];

        $this->assertSame('Polygon', $raster['bbox']['type']);
        // ST_MakeEnvelope produces a closed 5-vertex ring.
        $this->assertCount(5, $raster['bbox']['coordinates'][0]);
        $this->assertSame([-160.5, 55.1, -160.0, 55.4], $raster['bounds']);
        // A ~0.5° x 0.3° box at 55°N is a few hundred km² of ground, not a
        // number the page should print raw — but it must be positive and
        // sane, which is what catches a missing ::geography cast.
        $this->assertGreaterThan(500.0, $raster['extent_km2']);
        $this->assertLessThan(2000.0, $raster['extent_km2']);
    }

    public function test_a_raster_that_could_not_be_reprojected_has_no_footprint_and_is_counted(): void
    {
        // CRS present, bbox NULL — raster_parser's `reprojection_failed`
        // path. Indexed but unplaceable, which looks identical to "not
        // georeferenced" unless it is counted on its own.
        $this->insertRaster([
            'bounds' => [],
            'warnings' => [['code' => 'reprojection_failed', 'message' => 'Could not reproject bounds to EPSG:4326.']],
        ]);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/rasters")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('rasters.0.bbox', null)
                    ->where('rasters.0.bounds', null)
                    ->where('rasters.0.georeferenced', true)
                    ->where('rasters.0.warning_count', 1)
                    ->where('summary.missing_footprint', 1)
                    ->where('summary.with_warnings', 1),
            );
    }

    public function test_a_raster_with_no_crs_is_flagged_for_georeferencing(): void
    {
        $this->insertRaster([
            'crs' => null,
            'crs_confidence' => null,
            'bounds' => [],
            'sha' => str_repeat('b', 64),
        ]);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/rasters")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('rasters.0.crs', null)
                    ->where('rasters.0.georeferenced', false)
                    // No CRS means we cannot know it is a measurement grid,
                    // and the Python mirror returns False for the same reason.
                    ->where('rasters.0.ocr_skipped', false)
                    ->where('summary.missing_crs', 1)
                    ->where('summary.georeferenced', 0),
            );
    }

    public function test_a_measurement_grid_is_marked_as_never_sent_to_ocr(): void
    {
        // float32 bands + a CRS is exactly what tiff_normalize step 2c stops
        // before the PDF wrap, so this file has no document and no passages.
        $this->insertRaster([
            'layer_name' => 'Unga_magnetics_RTP',
            'band_stats' => [
                ['band_index' => 1, 'dtype' => 'float32', 'min' => -412.5, 'max' => 918.2, 'mean' => 3.1, 'nodata' => -9999, 'description' => 'nT'],
            ],
        ]);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/rasters")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('rasters.0.ocr_skipped', true)
                    ->where('summary.ocr_skipped', 1),
            );
    }

    public function test_an_eight_bit_scanned_sheet_is_not_marked_as_skipped(): void
    {
        // The conservative direction of _is_measurement_raster: a scanned
        // sheet that was later georeferenced is ADR-0005's actual target and
        // must not be labelled "no text".
        $this->insertRaster();

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/rasters")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('rasters.0.ocr_skipped', false)
                    ->where('summary.ocr_skipped', 0),
            );
    }

    public function test_a_raster_in_another_workspace_is_not_listed(): void
    {
        $this->insertRaster(['layer_name' => 'Ours']);

        $otherWorkspace = $this->makeWorkspace('Other Workspace');
        $otherProject = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$otherWorkspace, $otherProject->project_id],
        );
        $this->insertRaster([
            'layer_name' => 'Theirs',
            'sha' => str_repeat('c', 64),
            'project_id' => $otherProject->project_id,
            'workspace_id' => $otherWorkspace,
        ]);

        // Belt-and-braces: a row carrying OUR project_id but a foreign
        // workspace_id. A project-only filter would leak this one.
        $this->insertRaster([
            'layer_name' => 'Mislabelled',
            'sha' => str_repeat('d', 64),
            'workspace_id' => $otherWorkspace,
        ]);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/rasters")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->has('rasters', 1)
                    ->where('rasters.0.layer_name', 'Ours'),
            );
    }

    public function test_a_project_with_no_workspace_is_refused_rather_than_read_unscoped(): void
    {
        // The RLS policy is permissive on an empty GUC, so reading this
        // project "scoped" to '' would return every workspace's rasters.
        DB::statement(
            'UPDATE silver.projects SET workspace_id = NULL WHERE project_id = ?::uuid',
            [$this->project->project_id],
        );

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/rasters")
            ->assertStatus(409);
    }

    public function test_a_wrapped_tiff_with_no_raster_row_is_listed_as_needing_georeferencing(): void
    {
        // Ingested with a CRS: raster row written, sha8 matches the derived
        // key, so it is NOT in the needs-georeferencing list.
        $this->insertRaster(['sha' => str_repeat('a', 64)]);
        $this->insertDerivedTiffReport(substr(str_repeat('a', 64), 0, 8), 'Georeferenced_Sheet');

        // Ingested with no CRS: persist_raster_metadata returned early and
        // wrote nothing at all, so the only trace is this report row.
        $orphan = $this->insertDerivedTiffReport('deadbeef', 'Unga_Plan_1974');

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/rasters")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->has('ungeoreferenced', 1)
                    ->where('ungeoreferenced.0.report_id', $orphan)
                    ->where('ungeoreferenced.0.source_filename', 'Unga_Plan_1974'),
            );
    }

    public function test_a_plain_pdf_report_is_never_listed_as_needing_georeferencing(): void
    {
        DB::table('silver.reports')->insert([
            'report_id' => (string) Str::uuid(),
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->project->project_id,
            'title' => 'NI 43-101 Madsen PFS',
            'parser_used' => 'fitz',
            'is_scanned' => false,
            'version' => 1,
            'qp_name' => '{}',
            'source_object_key' => "reports/{$this->project->project_id}/20260820_155245_madsen.pdf",
        ]);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/rasters")
            ->assertOk()
            ->assertInertia(fn (AssertableInertia $page) => $page->has('ungeoreferenced', 0));
    }

    public function test_the_derived_key_filename_helper_recovers_the_uploaded_stem(): void
    {
        $this->assertSame(
            'Unga_Plan_1974',
            RasterLayersController::filenameFromDerivedTiffKey(
                'reports/2f1c/tiff-derived-deadbeef-Unga_Plan_1974.pdf',
            ),
        );

        // Not a shape we mint — keep the segment rather than returning null,
        // so the operator still has something to search bronze for.
        $this->assertSame(
            'something-else.pdf',
            RasterLayersController::filenameFromDerivedTiffKey('reports/2f1c/something-else.pdf'),
        );

        $this->assertNull(RasterLayersController::filenameFromDerivedTiffKey(null));
    }
}
