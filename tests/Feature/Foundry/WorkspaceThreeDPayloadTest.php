<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Http\Controllers\Foundry\WorkspaceController;
use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Inertia\Testing\AssertableInertia;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * WorkspaceThreeDPayloadTest — pins the Inertia prop keys consumed by the
 * 3D mode of resources/js/Pages/Foundry/Workspace.tsx so a future
 * refactor of WorkspaceController doesn't silently break a sub-view.
 *
 * Nine sub-views as of 2026-05-25:
 *   - Lithology              → first_holes_intervals / intervals_count
 *   - Trajectories           → surveys_3d
 *   - Spiral                 → surveys_3d (filtered per active hole)
 *   - Stereosphere           → structures_3d
 *   - Project Stereonet      → structures_3d
 *   - Assay Grade            → assay_composites_3d / assay_elements_3d
 *   - Significant            → significant_intersections_3d
 *   - Structure Discs        → structures_visual_3d
 *   - Commodity Samples      → commodity_samples_3d / commodity_keys_3d
 *
 * 2026-08-19 — REWRITTEN to seed its own fixture.
 *
 * Every one of these five tests was skipping. The file deliberately avoided
 * RefreshDatabase and asserted against "whatever project already exists in
 * the connected Postgres test DB", on the reasoning that building fixtures
 * would be heavy for a prop-key smoke test. Under RefreshDatabase-based
 * siblings the test DB is empty at this point, so `Project::query()->first()`
 * returned null and all five hit `markTestSkipped('No projects in DB.')`.
 * The suite reported green. The 1,127-line controller these tests exist to
 * protect had, in practice, no coverage at all — which is worse than having
 * no test, because the green tick actively said otherwise.
 *
 * The stated cost turned out not to be real: seeding a workspace, a project
 * and two collars runs in well under a second, and the prop-key assertions
 * do not need populated child tables — an empty `assay_composites_3d` still
 * proves the key is emitted, which is the whole contract being pinned.
 *
 * RequiresPostgres stays: the 3D queries use ST_X and jsonb operators that
 * would not run under the SQLite fast suite.
 *
 * One incidental proof that these tests had never executed: every one of
 * them called `$project->users()`, a relation App\Models\Project does not
 * define. The first line of the first test would have thrown
 * BadMethodCallException. Membership is attached from the other side,
 * `$user->projects()`, as the sibling Foundry tests do.
 */
final class WorkspaceThreeDPayloadTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    /**
     * @return array{user: User, project: Project}
     */
    private function seedProjectWithCollars(int $collarCount = 2): array
    {
        $user = User::factory()->create();

        $workspaceId = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$workspaceId, '3D Payload Test Workspace', 'w3d-'.substr($workspaceId, 0, 8)],
        );

        $project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$workspaceId, $project->project_id],
        );
        $user->projects()->syncWithoutDetaching([$project->project_id => ['role' => 'viewer']]);

        for ($i = 1; $i <= $collarCount; $i++) {
            DB::statement(
                "INSERT INTO silver.collars (
                    collar_id, hole_id, project_id, workspace_id,
                    easting, northing, elevation, total_depth, azimuth, dip,
                    hole_type, status, geom_4326
                 ) VALUES (
                    ?::uuid, ?, ?::uuid, ?::uuid,
                    500000, 4500000, 1000, 150, 180, -60,
                    'DDH', 'completed',
                    ST_Transform(ST_SetSRID(ST_MakePoint(500000, 4500000), 32613), 4326)
                 )",
                [(string) Str::uuid(), 'W3D-'.$i, $project->project_id, $workspaceId],
            );
        }

        return ['user' => $user, 'project' => $project];
    }

    /**
     * A collar whose file had no elevation is drawn at the terrain model's
     * ground height (silver.collars.elevation_dem_m, written by
     * promote_silver_to_gold) instead of z = 0, and says so; a surveyed
     * elevation always wins over the terrain value.
     */
    public function test_collar_elevation_falls_back_to_the_terrain_model(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProjectWithCollars(3);
        DB::statement(
            "UPDATE silver.collars
                SET elevation = NULL, elevation_dem_m = 53.5, elevation_dem_source = 'copernicus_glo30',
                    elevation_dem_geom = geom_4326
              WHERE project_id = ?::uuid AND hole_id = 'W3D-1'",
            [$project->project_id],
        );
        DB::statement(
            "UPDATE silver.collars
                SET elevation = NULL, elevation_dem_m = 99.0, elevation_dem_source = 'copernicus_glo30',
                    elevation_dem_geom = ST_SetSRID(ST_MakePoint(-150.0, 60.0), 4326)
              WHERE project_id = ?::uuid AND hole_id = 'W3D-3'",
            [$project->project_id],
        );
        // W3D-2 keeps its surveyed 1000 m; a stale terrain value must not show.
        DB::statement(
            "UPDATE silver.collars SET elevation_dem_m = 12.0 WHERE project_id = ?::uuid AND hole_id = 'W3D-2'",
            [$project->project_id],
        );

        $response = $this->actingAs($user)->get('/projects/'.$project->slug.'/workspace');

        $response->assertStatus(200);
        $response->assertInertia(
            fn (AssertableInertia $page) => $page
                ->component('Foundry/Workspace')
                ->where('collars.0.hole_id', 'W3D-1')
                ->where('collars.0.elevation', 53.5)
                ->where('collars.0.elevation_source', 'terrain')
                ->where('collars.1.hole_id', 'W3D-2')
                ->where('collars.1.elevation', 1000)
                ->where('collars.1.elevation_source', 'file')
                ->where('collars.0.elevation_dem_source', 'copernicus_glo30')
                ->where('collars.1.elevation_dem_source', null)
                // W3D-3: a terrain height looked up at a position the collar
                // has since left is stale and is not served as its elevation.
                ->where('collars.2.hole_id', 'W3D-3')
                ->where('collars.2.elevation', null)
                ->where('collars.2.elevation_source', null)
                ->etc(),
        );
    }

    public function test_workspace_emits_every_3d_subview_prop_key(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProjectWithCollars();

        $response = $this->actingAs($user)->get('/projects/'.$project->slug.'/workspace');

        $response->assertStatus(200);
        $response->assertInertia(
            fn (AssertableInertia $page) => $page
                ->component('Foundry/Workspace')
                ->has('intervals_count')
                // FE-11: the heavy 3D payload is a deferred group, NOT part
                // of the initial page — MAP mode never reads it.
                ->missing('first_holes_intervals')
                ->missing('surveys_3d')
                ->loadDeferredProps('viz3d', fn (AssertableInertia $reload) => $reload
                    ->has('first_holes_intervals')
                    ->has('surveys_3d')
                    ->has('structures_3d')
                    ->has('assay_composites_3d')
                    ->has('assay_elements_3d')
                    ->has('significant_intersections_3d')
                    ->has('structures_visual_3d')
                    ->has('commodity_samples_3d')
                    ->has('commodity_keys_3d')
                    ->has('survey_holes_downsampled')),
        );
    }

    /**
     * GIS audit 2026-10 (finding 10): a structure with no orientation was sent
     * to the discs as dip 0 / strike 0 / trend 0 - a flat plane - and to the
     * Workspace stereonet as a horizontal bed at the centre of the net. A NULL
     * is not a 0; the row is dropped.
     */
    public function test_structure_discs_drop_measurements_without_an_orientation(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProjectWithCollars(1);
        $collar = DB::table('silver.collars')->where('project_id', $project->project_id)->first();

        $insert = function (float $depth, ?float $dip, ?float $dipDir, ?float $strike) use ($collar, $project): void {
            DB::statement(
                "INSERT INTO gold.structure_measurements_visual (
                    collar_id, workspace_id, project_id, depth, structure_type,
                    strike_deg, dip_deg, dip_direction_deg, projection
                 ) VALUES (?::uuid, ?::uuid, ?::uuid, ?, 'bedding', ?, ?, ?, 'equal_area')",
                [$collar->collar_id, $collar->workspace_id, $project->project_id, $depth, $strike, $dip, $dipDir],
            );
        };
        $insert(10.0, 30.0, 120.0, 30.0);   // fully oriented
        $insert(20.0, null, null, null);    // no orientation at all
        $insert(30.0, 45.0, null, null);    // a dip with no dip direction
        $insert(40.0, 0.0, 90.0, 0.0);      // genuinely horizontal: dip 0 is a value, not a gap

        $this->actingAs($user)
            ->get('/projects/'.$project->slug.'/workspace')
            ->assertInertia(fn (AssertableInertia $page) => $page->loadDeferredProps('viz3d', fn (AssertableInertia $reload) => $reload
                ->has('structures_visual_3d', 2)
                ->where('structures_visual_3d.0.depth_m', 10)
                ->where('structures_visual_3d.0.dip_deg', 30)
                ->where('structures_visual_3d.0.pole_trend_deg', 300)
                ->where('structures_visual_3d.0.pole_plunge_deg', 60)
                ->where('structures_visual_3d.1.depth_m', 40)
                ->where('structures_visual_3d.1.dip_deg', 0)
                ->where('structures_visual_3d.1.pole_plunge_deg', 90)));
    }

    public function test_structure_disc_row_keeps_a_zero_dip_and_drops_a_null_one(): void
    {
        $row = static fn (?float $dip, ?float $dipDir, ?float $strike = null): object => (object) [
            'collar_id' => 'c', 'structure_type' => 'joint', 'depth' => 5.0,
            'dip_deg' => $dip, 'dip_direction_deg' => $dipDir, 'strike_deg' => $strike,
            'trend_deg' => null, 'plunge_deg' => null,
        ];

        $flat = WorkspaceController::structureDiscRow($row(0.0, 0.0));
        $this->assertNotNull($flat, 'a horizontal bed is a measurement');
        $this->assertSame(0.0, $flat['dip_deg']);
        $this->assertSame(90.0, $flat['pole_plunge_deg']);
        $this->assertSame(180.0, $flat['pole_trend_deg']);
        $this->assertSame(270.0, $flat['strike_deg'], 'right-hand rule: dip direction - 90');

        $this->assertNull(WorkspaceController::structureDiscRow($row(null, 120.0)));
        $this->assertNull(WorkspaceController::structureDiscRow($row(30.0, null)));
        $this->assertNull(WorkspaceController::structureDiscRow($row(null, null)));
    }

    /**
     * A project at 58 N, -102 E (UTM 14N; its project grid, EPSG:26913, is a
     * different projection) with one collar recorded at azimuth 90 and
     * stations at azimuth 90. The expected numbers are the ones the Python
     * promote step implies; see tests/Unit/Services/Collars/SurveyAzimuthReferenceTest.php
     * and src/fastapi/tests/test_azimuth_reference_php_parity.py.
     *
     * @param list<array{0: float, 1: ?string}> $stations [depth, own azimuth_reference]
     *
     * @return array{user: User, project: Project, collar_id: string}
     */
    private function seedAzimuthProject(?string $reference, ?float $declination, ?int $crsEpsg, array $stations): array
    {
        ['user' => $user, 'project' => $project] = $this->seedProjectWithCollars(0);
        DB::statement(
            'UPDATE silver.projects SET orientation_reference = ?, magnetic_declination = ?, crs_epsg = ? WHERE project_id = ?::uuid',
            [$reference ?? 'BOH', $declination, $crsEpsg, $project->project_id],
        );
        $collarId = (string) Str::uuid();
        $workspaceId = (string) DB::table('silver.projects')->where('project_id', $project->project_id)->value('workspace_id');
        DB::statement(
            "INSERT INTO silver.collars (
                collar_id, hole_id, project_id, workspace_id, easting, northing, elevation,
                total_depth, azimuth, dip, hole_type, status, geom_4326
             ) VALUES (?::uuid, 'AZ-1', ?::uuid, ?::uuid, 0, 0, 300, 100, 90, -60, 'DDH', 'completed',
                       ST_SetSRID(ST_MakePoint(-102, 58), 4326))",
            [$collarId, $project->project_id, $workspaceId],
        );
        foreach ($stations as [$depth, $own]) {
            DB::statement(
                "INSERT INTO silver.surveys (survey_id, collar_id, workspace_id, depth, azimuth, dip, survey_method, azimuth_reference)
                 VALUES (gen_random_uuid(), ?::uuid, ?::uuid, ?, 90, -60, 'downhole', ?)",
                [$collarId, $workspaceId, $depth, $own],
            );
        }

        return ['user' => $user, 'project' => $project, 'collar_id' => $collarId];
    }

    /**
     * @return array{collars: list<array<string, mixed>>, surveys: list<array<string, mixed>>}
     */
    private function fetchAzimuths(User $user, Project $project): array
    {
        $out = ['collars' => [], 'surveys' => []];
        $this->actingAs($user)
            ->get('/projects/'.$project->slug.'/workspace')
            ->assertInertia(function (AssertableInertia $page) use (&$out): void {
                $page->where('collars', function ($collars) use (&$out): bool {
                    $out['collars'] = array_map(fn ($c) => (array) $c, iterator_to_array($collars));

                    return true;
                });
                $page->loadDeferredProps('viz3d', function (AssertableInertia $reload) use (&$out): void {
                    $reload->where('surveys_3d', function ($surveys) use (&$out): bool {
                        $out['surveys'] = array_map(fn ($s) => (array) $s, iterator_to_array($surveys));

                        return true;
                    });
                });
            });

        return $out;
    }

    /**
     * GIS audit 2026-10 (finding 5): promote_silver_to_gold converts a
     * declared azimuth reference before it desurveys the map trace; the 3D
     * payload sent the raw numbers, so a magnetic or non-local-grid hole was
     * drawn rotated against its own trace by the whole declination or
     * convergence. Station azimuths now arrive relative to TRUE north (the 3D
     * frame's north): the station's own reference wins, then the project's.
     */
    public function test_declared_azimuth_references_reach_the_3d_payload_as_true_north_azimuths(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedAzimuthProject(
            reference: 'grid',
            declination: 12.0,
            crsEpsg: 26913,
            stations: [[10.0, null], [20.0, 'true'], [30.0, 'magnetic'], [40.0, 'grid']],
        );

        $got = $this->fetchAzimuths($user, $project);
        $byDepth = [];
        foreach ($got['surveys'] as $s) {
            $byDepth[(int) $s['depth']] = $s;
        }

        // No reference of its own: the project's (grid north of EPSG:26913,
        // where true north is 2.5448022 degrees anticlockwise of grid north).
        $this->assertEqualsWithDelta(92.544802210, $byDepth[10]['azimuth'], 1e-6);
        $this->assertSame(90.0, (float) $byDepth[10]['azimuth_recorded']);
        $this->assertSame('grid', $byDepth[10]['azimuth_reference']);
        // Its own 'true' wins over the project's grid: already true north.
        $this->assertSame(90.0, (float) $byDepth[20]['azimuth']);
        $this->assertSame('true', $byDepth[20]['azimuth_reference']);
        $this->assertArrayNotHasKey('azimuth_recorded', $byDepth[20]);
        // Its own 'magnetic' + the project's 12 degrees east declination.
        $this->assertEqualsWithDelta(102.0, $byDepth[30]['azimuth'], 1e-9);
        $this->assertSame(90.0, (float) $byDepth[30]['azimuth_recorded']);
        // Its own 'grid' is the project's grid too.
        $this->assertEqualsWithDelta(92.544802210, $byDepth[40]['azimuth'], 1e-6);
        foreach ($byDepth as $s) {
            $this->assertArrayNotHasKey('azimuth_unapplied', $s);
        }

        // The collar's own azimuth takes the project's declaration.
        $this->assertEqualsWithDelta(92.544802210, $got['collars'][0]['azimuth'], 1e-6);
        $this->assertSame(90.0, (float) $got['collars'][0]['azimuth_recorded']);
        $this->assertFalse($got['collars'][0]['azimuth_unapplied']);
    }

    public function test_a_declared_grid_with_no_project_crs_uses_the_collars_own_utm_zone(): void
    {
        // promote: a project CRS that is missing, or the collar's own zone,
        // leaves the grid azimuth alone - the collar's zone IS the grid. Here
        // that is UTM 14N (EPSG:32614), where true north is 2.5448022
        // degrees CLOCKWISE of grid north. 99999 is not in spatial_ref_sys.
        foreach ([null, 32614, 99999] as $crs) {
            ['user' => $user, 'project' => $project] = $this->seedAzimuthProject('grid', null, $crs, [[10.0, null]]);
            $got = $this->fetchAzimuths($user, $project);
            $this->assertEqualsWithDelta(87.455197790, $got['surveys'][0]['azimuth'], 1e-6, 'project crs_epsg '.var_export($crs, true));
        }
    }

    public function test_a_magnetic_reference_without_a_declination_is_flagged_not_guessed(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedAzimuthProject('magnetic', null, null, [[10.0, null], [20.0, 'true']]);

        $got = $this->fetchAzimuths($user, $project);

        $this->assertSame(90.0, (float) $got['surveys'][0]['azimuth']);
        $this->assertTrue($got['surveys'][0]['azimuth_unapplied']);
        // A station that declares its own reference is not held up by the project's gap.
        $this->assertSame(90.0, (float) $got['surveys'][1]['azimuth']);
        $this->assertArrayNotHasKey('azimuth_unapplied', $got['surveys'][1]);
        $this->assertTrue($got['collars'][0]['azimuth_unapplied']);
        $this->assertSame(90.0, (float) $got['collars'][0]['azimuth']);
    }

    public function test_an_undeclared_project_sends_the_recorded_azimuths_untouched(): void
    {
        // BOH / TOH are the core-orientation mark: they declare no north.
        foreach (['BOH', 'TOH'] as $mark) {
            ['user' => $user, 'project' => $project] = $this->seedAzimuthProject($mark, 12.0, 26913, [[10.0, null]]);
            $got = $this->fetchAzimuths($user, $project);

            $this->assertSame(90.0, (float) $got['surveys'][0]['azimuth']);
            foreach (['azimuth_reference', 'azimuth_recorded', 'azimuth_unapplied'] as $key) {
                $this->assertArrayNotHasKey($key, $got['surveys'][0], $mark.' must not add '.$key);
            }
            $this->assertSame(90.0, (float) $got['collars'][0]['azimuth']);
            $this->assertNull($got['collars'][0]['azimuth_recorded']);
            $this->assertFalse($got['collars'][0]['azimuth_unapplied']);
        }
    }

    /**
     * The 3D fallback path: when silver.surveys is empty for a hole but
     * AZIMUTH + SANG curves exist in silver.well_log_curves, the
     * controller should derive station rows on the fly so Trajectories
     * + Spiral light up. We can't easily assert "fallback was used" from
     * outside the controller, but we can assert every survey row in the
     * payload has a numeric depth + azimuth + dip — both real and
     * derived rows share the same shape.
     */
    public function test_surveys_3d_rows_have_required_keys(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProjectWithCollars();

        $response = $this->actingAs($user)->get('/projects/'.$project->slug.'/workspace');

        $response->assertInertia(
            fn (AssertableInertia $page) => $page->loadDeferredProps('viz3d', fn (AssertableInertia $reload) => $reload->where(
                'surveys_3d',
                function ($surveys) {
                    if (! is_array($surveys) || count($surveys) === 0) {
                        return true;
                    }
                    foreach ($surveys as $s) {
                        $arr = (array) $s;
                        foreach (['collar_id', 'depth', 'azimuth', 'dip'] as $key) {
                            if (! array_key_exists($key, $arr)) {
                                return false;
                            }
                        }
                    }

                    return true;
                },
            )),
        );
    }

    /**
     * §04e (2026-09-29): up-holes are legal and lib/desurvey.ts now honours
     * the sign of dip, so a station derived from a SANG curve (0 = vertical
     * down, 90 = horizontal) must arrive in the silver convention — negative
     * = down, dip = SANG - 90. It used to be 90 - SANG, which the
     * integrator only drew downward because it ignored the sign; with the
     * sign honoured every Cameco hole would have been drawn going UP.
     */
    public function test_sang_derived_stations_use_the_down_negative_convention(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProjectWithCollars(1);
        $collar = DB::table('silver.collars')->where('project_id', $project->project_id)->first();

        foreach (['AZIMUTH' => '{180,181,182}', 'SANG' => '{0,10,30}'] as $name => $values) {
            DB::statement(
                "INSERT INTO silver.well_log_curves (
                    curve_id, collar_id, workspace_id, curve_name, min_depth, max_depth,
                    null_value, sample_count, depths, \"values\", created_at, updated_at
                 ) VALUES (?::uuid, ?::uuid, ?::uuid, ?, 0, 20, -999.25, 3, '{0,10,20}', ?::float8[], NOW(), NOW())",
                [(string) Str::uuid(), $collar->collar_id, $collar->workspace_id, $name, $values],
            );
        }

        $response = $this->actingAs($user)->get('/projects/'.$project->slug.'/workspace');

        $response->assertInertia(
            fn (AssertableInertia $page) => $page->loadDeferredProps('viz3d', fn (AssertableInertia $reload) => $reload->where(
                'surveys_3d',
                function ($surveys): bool {
                    $dips = [];
                    foreach ($surveys as $s) {
                        $s = (array) $s;
                        // JSON-decoded by the Inertia assertion: -90.0 arrives as -90.
                        $dips[(string) $s['depth']] = (float) $s['dip'];
                    }
                    ksort($dips);

                    return $dips === ['0' => -90.0, '10' => -80.0, '20' => -60.0];
                },
            )),
        );
    }

    /**
     * Regression for the 2026-08-17 restore: WorkspaceController now wraps
     * its ~24 query blocks in withWorkspaceRls(), including the
     * silver.saved_map_views count that drives the "Saved views" layer.
     * That table is fail-closed RLS (second RLS pass, 2026-08-15) — a
     * missing/incorrect wrap would silently render as 0 for every project,
     * indistinguishable from "no saved views exist" on a page that never
     * asserts the count directly. Assert the route renders end-to-end
     * (proves the RLS wrap doesn't throw) as a floor; the exact count
     * depends on whatever the connected Postgres test DB's first project
     * has, which this test doesn't control.
     */
    public function test_workspace_renders_with_project_layers_prop(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProjectWithCollars();

        $response = $this->actingAs($user)->get('/projects/'.$project->slug.'/workspace');

        $response->assertStatus(200);
        $response->assertInertia(
            fn (AssertableInertia $page) => $page
                ->component('Foundry/Workspace')
                ->has('project_layers'),
        );
    }

    /**
     * FE-5 / FE-3: the map needs the tile cache key and, for a project with
     * no positioned collars, an extent to open on.
     */
    public function test_workspace_sends_data_version_and_project_extent(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProjectWithCollars(0);
        DB::statement('UPDATE silver.projects SET data_version = 7 WHERE project_id = ?::uuid', [$project->project_id]);

        $this->actingAs($user)
            ->get('/projects/'.$project->slug.'/workspace')
            ->assertStatus(200)
            ->assertInertia(fn (AssertableInertia $page) => $page
                ->where('project.data_version', 7)
                // No collars and no other map data: null, and the client
                // falls back to its default view instead of no map.
                ->where('project_extent', null)
                ->where('empty', true));
    }

    /**
     * WorkspaceController::holePayload() — the compare-modal JSON endpoint
     * — had no test coverage in the original deleted test file. Added on
     * restore. Also proves its own withWorkspaceRls() wrap doesn't break
     * the happy path.
     */
    public function test_hole_payload_returns_json_for_a_real_collar(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProjectWithCollars();

        $collar = DB::table('silver.collars')
            ->where('project_id', $project->project_id)
            ->first();
        $this->assertNotNull($collar, 'fixture must have seeded a collar');

        $hole = $collar->hole_id_canonical ?? $collar->hole_id;
        $response = $this->actingAs($user)
            ->get('/projects/'.$project->slug.'/holes/'.$hole.'/payload');

        $response->assertStatus(200);
        $response->assertJsonStructure([
            'hole_id', 'collar_id', 'total_depth', 'easting', 'northing',
            'lat', 'lng', 'log_tracks', 'log_depth_max', 'lithology_intervals',
            'ore_bands', 'ore_thickness_m', 'mean_u3o8_pct',
        ]);
    }

    public function test_hole_payload_404s_for_unknown_hole(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProjectWithCollars();

        $response = $this->actingAs($user)
            ->get('/projects/'.$project->slug.'/holes/DOES-NOT-EXIST/payload');

        $response->assertStatus(404);
    }
}
