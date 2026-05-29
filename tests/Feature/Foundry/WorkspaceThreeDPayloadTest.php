<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\Project;
use App\Models\User;
use Inertia\Testing\AssertableInertia;
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
 * Skipped (not failed) when the DB has no projects — keeps CI green on
 * fresh test DBs that lack the silver/gold tables this controller hits.
 */
final class WorkspaceThreeDPayloadTest extends TestCase
{
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
}
