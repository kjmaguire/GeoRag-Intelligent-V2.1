<?php

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

/**
 * `silver.projects.crs_epsg` must be writable, not just readable.
 *
 * The column has existed since the schema was written and FOUR readers
 * consult it — Overview, Workspace, Chat's context envelope and the projects
 * index. Nothing had ever been able to WRITE it: it was absent from
 * StoreProjectRequest::rules() and from Project::$fillable, so mass
 * assignment dropped it silently even when a client sent it. Every project
 * in the live database had crs_epsg NULL.
 *
 * That is not cosmetic. ingest_tabular now resolves a drill table's
 * coordinate system as:
 *
 *     1. the per-file source_epsg the import wizard collects
 *     2. THIS column
 *     3. DEFAULT_SOURCE_EPSG — 32613, UTM zone 13N, which runs through
 *        Colorado
 *
 * With (2) permanently NULL an Alaskan project read its collar CSVs as zone
 * 13 and wrote the holes roughly 2,500 km east of where they were drilled,
 * with nothing but a warning to say so. Nothing in a CSV or a spreadsheet
 * declares a projection, so there is no third place the right answer could
 * have come from.
 *
 * 26904 throughout: NAD83 / UTM zone 4N, the Alaska Peninsula, the delivery
 * that surfaced this.
 */
class ProjectCrsEpsgTest extends TestCase
{
    use RefreshDatabase;

    private User $user;

    protected function setUp(): void
    {
        parent::setUp();
        Project::getModel()->setTable('projects');
        // An admin, per ProjectControllerTest::actingAsAdmin(). Project
        // creation is gated on already belonging to a workspace (the A2-01
        // fix — a stranger's first API call used to land inside the live
        // tenant), and an admin bootstrapping a fresh deployment is the one
        // caller allowed through without a membership.
        $this->user = User::factory()->create(['is_admin' => true]);
        $this->actingAs($this->user);
    }

    public function test_crs_epsg_is_persisted_on_create(): void
    {
        $response = $this->postJson('/api/v1/projects', [
            'project_name' => 'Apollo Sitka',
            'crs_epsg' => 26904,
            'orientation_reference' => 'BOH',
        ]);

        $response->assertCreated();

        $project = Project::where('project_name', 'Apollo Sitka')->firstOrFail();
        $this->assertSame(
            26904,
            (int) $project->crs_epsg,
            'crs_epsg must survive mass assignment — it is the fallback CRS '
            .'for every drill table uploaded into this project',
        );
    }

    public function test_it_is_optional(): void
    {
        // A project whose CRS is not known yet is legitimate; the ingest
        // warns loudly rather than refusing, so the field must not become a
        // required one and block project creation.
        $this->postJson('/api/v1/projects', [
            'project_name' => 'No CRS',
            'orientation_reference' => 'BOH',
        ])->assertCreated();

        $project = Project::where('project_name', 'No CRS')->firstOrFail();
        $this->assertNull($project->crs_epsg);
    }

    /**
     * The bound is not invented here.
     *
     * `min:1024, max:32767` is the rule already written in
     * StoreQueryRequest (context_envelope.crs_epsg) and in the CHECK on
     * silver.spatial_features.crs_epsg_native. A fourth definition of "a
     * valid CRS" is how the three that already exist would drift.
     */
    public function test_it_rejects_an_epsg_outside_the_accepted_range(): void
    {
        foreach ([1023, 32768, 0, -1] as $bad) {
            $this->postJson('/api/v1/projects', [
                'project_name' => "Bad {$bad}",
                'crs_epsg' => $bad,
            ])->assertStatus(422)->assertJsonValidationErrors('crs_epsg');
        }
    }

    public function test_it_accepts_both_ends_of_the_range(): void
    {
        foreach ([1024, 32767] as $edge) {
            $this->postJson('/api/v1/projects', [
                'project_name' => "Edge {$edge}",
                'crs_epsg' => $edge,
                'orientation_reference' => 'BOH',
            ])->assertCreated();
        }
    }

    public function test_it_rejects_a_non_integer(): void
    {
        // 'EPSG:26904' is the spelling a user would reach for, and the one
        // the UI explicitly tells them not to type. It must fail loudly
        // rather than cast to 0 and store a nonsense CRS.
        $this->postJson('/api/v1/projects', [
            'project_name' => 'Prefixed',
            'crs_epsg' => 'EPSG:26904',
        ])->assertStatus(422)->assertJsonValidationErrors('crs_epsg');
    }

    public function test_crs_datum_and_crs_epsg_are_independent(): void
    {
        // crs_datum is free text and cannot be reliably parsed back into a
        // number to reproject with, which is why a separate integer column
        // exists at all. Setting one must not clobber the other.
        $this->postJson('/api/v1/projects', [
            'project_name' => 'Both',
            'crs_datum' => 'NAD83 / UTM zone 4N',
            'crs_epsg' => 26904,
            'orientation_reference' => 'BOH',
        ])->assertCreated();

        $project = Project::where('project_name', 'Both')->firstOrFail();
        $this->assertSame('NAD83 / UTM zone 4N', $project->crs_datum);
        $this->assertSame(26904, (int) $project->crs_epsg);
    }
}
