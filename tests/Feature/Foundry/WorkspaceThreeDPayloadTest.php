<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

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
