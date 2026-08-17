<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\Project;
use App\Models\User;
use Illuminate\Support\Facades\DB;
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
 * Deliberately does NOT use RefreshDatabase — it asserts against whatever
 * project already exists in the connected Postgres test DB (building
 * realistic collars/surveys/assay-composite/structure fixtures for every
 * run would be heavy for a prop-key-shape smoke test). Skipped when no
 * project exists yet, same as always.
 *
 * 2026-08-17 — restored after the 2026-07-29 reader-core trim; added
 * RequiresPostgres alongside the pre-existing null-project skip (kept
 * as-is: it's the real gate under a freshly-seeded Postgres DB, while
 * RequiresPostgres additionally guards the 3D queries' Postgres-only SQL
 * — ST_X, jsonb operators — from ever running under the default SQLite
 * suite).
 */
final class WorkspaceThreeDPayloadTest extends TestCase
{
    use RequiresPostgres;

    public function test_workspace_emits_every_3d_subview_prop_key(): void
    {
        $project = Project::query()->first();
        if (! $project) {
            $this->markTestSkipped('No projects in DB.');
        }

        $user = User::factory()->create();
        $project->users()->syncWithoutDetaching([$user->id => ['role' => 'viewer']]);

        $response = $this->actingAs($user)->get('/projects/'.$project->slug.'/workspace');

        $response->assertStatus(200);
        $response->assertInertia(
            fn (AssertableInertia $page) => $page
                ->component('Foundry/Workspace')
                ->has('first_holes_intervals')
                ->has('intervals_count')
                ->has('surveys_3d')
                ->has('structures_3d')
                ->has('assay_composites_3d')
                ->has('assay_elements_3d')
                ->has('significant_intersections_3d')
                ->has('structures_visual_3d')
                ->has('commodity_samples_3d')
                ->has('commodity_keys_3d'),
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
        $project = Project::query()->first();
        if (! $project) {
            $this->markTestSkipped('No projects in DB.');
        }

        $user = User::factory()->create();
        $project->users()->syncWithoutDetaching([$user->id => ['role' => 'viewer']]);

        $response = $this->actingAs($user)->get('/projects/'.$project->slug.'/workspace');

        $response->assertInertia(
            fn (AssertableInertia $page) => $page->where(
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
            ),
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
        $project = Project::query()->first();
        if (! $project) {
            $this->markTestSkipped('No projects in DB.');
        }

        $user = User::factory()->create();
        $project->users()->syncWithoutDetaching([$user->id => ['role' => 'viewer']]);

        $response = $this->actingAs($user)->get('/projects/'.$project->slug.'/workspace');

        $response->assertStatus(200);
        $response->assertInertia(
            fn (AssertableInertia $page) => $page
                ->component('Foundry/Workspace')
                ->has('project_layers'),
        );
    }

    /**
     * WorkspaceController::holePayload() — the compare-modal JSON endpoint
     * — had no test coverage in the original deleted test file. Added on
     * restore. Also proves its own withWorkspaceRls() wrap doesn't break
     * the happy path.
     */
    public function test_hole_payload_returns_json_for_a_real_collar(): void
    {
        $project = Project::query()->first();
        if (! $project) {
            $this->markTestSkipped('No projects in DB.');
        }

        $collar = DB::table('silver.collars')
            ->where('project_id', $project->project_id)
            ->first();
        if (! $collar) {
            $this->markTestSkipped('No collars for this project.');
        }

        $user = User::factory()->create();
        $project->users()->syncWithoutDetaching([$user->id => ['role' => 'viewer']]);

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
        $project = Project::query()->first();
        if (! $project) {
            $this->markTestSkipped('No projects in DB.');
        }

        $user = User::factory()->create();
        $project->users()->syncWithoutDetaching([$user->id => ['role' => 'viewer']]);

        $response = $this->actingAs($user)
            ->get('/projects/'.$project->slug.'/holes/DOES-NOT-EXIST/payload');

        $response->assertStatus(404);
    }
}
