<?php

namespace Tests\Feature\Api\V1;

use App\Models\Collar;
use App\Models\Project;
use App\Models\Survey;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * Feature tests for CollarController.
 *
 * Scoped under a project — routes are /api/v1/projects/{project}/collars.
 *
 * IMPORTANT: After the A2-02 IDOR fix, every CollarController method requires
 * the authenticated user to have a pivot row for the parent project.
 * Tests that operate against $this->project must call
 * $this->user->projects()->attach(...) in setUp (done below) or per-test.
 */
class CollarControllerTest extends TestCase
{
    use RefreshDatabase;

    private Project $project;

    private User $user;

    protected function setUp(): void
    {
        parent::setUp();

        // CollarController uses PostGIS ST_X/ST_Transform in selectRaw — SQLite
        // has no geometry column nor spatial functions, so these tests can only
        // run against a real Postgres test connection.
        $this->skipIfSqlite();

        // SQLite schema prefix override (same pattern as ProjectControllerTest).
        Project::getModel()->setTable('projects');
        Collar::getModel()->setTable('collars');
        Survey::getModel()->setTable('surveys');

        $this->user = User::factory()->create();
        $this->project = Project::factory()->create();

        // Attach the user to the project so the hasProjectAccess gate (A2-02 fix)
        // passes for all tests that operate against $this->project.
        $this->user->projects()->attach($this->project->project_id, ['role' => 'owner']);

        // Routes are behind Sanctum auth. Authenticate for every test so
        // individual methods can focus on the resource behaviour. Use the
        // `sanctum` guard explicitly so ability-aware middleware resolves
        // the same way production requests do.
        $this->actingAs($this->user, 'sanctum');
    }

    // -------------------------------------------------------------------------
    // index
    // -------------------------------------------------------------------------

    public function test_index_returns_collars_for_project(): void
    {
        Collar::factory()->count(3)->create(['project_id' => $this->project->project_id]);

        $other = Project::factory()->create();
        Collar::factory()->count(2)->create(['project_id' => $other->project_id]);

        $response = $this->getJson("/api/v1/projects/{$this->project->project_id}/collars");

        $response->assertOk();
        $this->assertCount(3, $response->json('data'));
    }

    public function test_index_filters_by_hole_type(): void
    {
        Collar::factory()->create([
            'project_id' => $this->project->project_id,
            'hole_type' => 'Diamond',
        ]);
        Collar::factory()->create([
            'project_id' => $this->project->project_id,
            'hole_type' => 'RC',
        ]);

        $response = $this->getJson(
            "/api/v1/projects/{$this->project->project_id}/collars?hole_type=Diamond",
        );

        $response->assertOk();
        $this->assertCount(1, $response->json('data'));
        $this->assertSame('Diamond', $response->json('data.0.hole_type'));
    }

    public function test_index_filters_by_status(): void
    {
        Collar::factory()->create([
            'project_id' => $this->project->project_id,
            'status' => 'Active',
        ]);
        Collar::factory()->create([
            'project_id' => $this->project->project_id,
            'status' => 'Completed',
        ]);

        $response = $this->getJson(
            "/api/v1/projects/{$this->project->project_id}/collars?status=Completed",
        );

        $response->assertOk();
        $this->assertCount(1, $response->json('data'));
    }

    public function test_index_returns_404_for_nonexistent_project(): void
    {
        $response = $this->getJson('/api/v1/projects/00000000-0000-0000-0000-000000000000/collars');

        $response->assertNotFound();
    }

    // -------------------------------------------------------------------------
    // store
    // -------------------------------------------------------------------------

    public function test_store_creates_collar_and_returns_201(): void
    {
        $payload = [
            'hole_id' => 'DH-001',
            'easting' => 425000.5,
            'northing' => 6790000.0,
            'elevation' => 510.0,
            'total_depth' => 350.0,
            'hole_type' => 'Diamond',
            'azimuth' => 135.0,
            'dip' => -60.0,
            'drill_date' => '2024-03-15',
            'status' => 'Completed',
        ];

        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/collars",
            $payload,
        );

        $response->assertCreated()
            ->assertJsonPath('data.hole_id', 'DH-001')
            ->assertJsonPath('data.project_id', $this->project->project_id);
    }

    public function test_store_returns_422_when_hole_id_duplicated_in_project(): void
    {
        Collar::factory()->create([
            'project_id' => $this->project->project_id,
            'hole_id' => 'DH-001',
        ]);

        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/collars",
            [
                'hole_id' => 'DH-001',
                'easting' => 425000.5,
                'northing' => 6790000.0,
                'total_depth' => 350.0,
                'hole_type' => 'RC',
            ],
        );

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['hole_id']);
    }

    /**
     * §04e (SME-approved, 2026-09-29): one collar per (project, canonical
     * hole id). A separator/case variant is the same hole — refused with a
     * 422, not a 500 off the unique index.
     */
    public function test_store_returns_422_for_a_spelling_variant_of_an_existing_hole(): void
    {
        $this->postJson("/api/v1/projects/{$this->project->project_id}/collars", [
            'hole_id' => 'LEB-23-001',
            'easting' => 425000.5,
            'northing' => 6790000.0,
            'hole_type' => 'RC',
            'status' => 'Active',
        ])->assertCreated();

        $this->assertSame(
            'LEB23001',
            DB::table('silver.collars')->where('hole_id', 'LEB-23-001')->value('hole_id_canonical'),
        );

        $this->postJson("/api/v1/projects/{$this->project->project_id}/collars", [
            'hole_id' => 'leb 23 001',
            'easting' => 425000.5,
            'northing' => 6790000.0,
            'hole_type' => 'RC',
            'status' => 'Active',
        ])->assertUnprocessable()->assertJsonValidationErrors(['hole_id']);
    }

    public function test_store_allows_same_hole_id_in_different_projects(): void
    {
        $other = Project::factory()->create();
        Collar::factory()->create([
            'project_id' => $other->project_id,
            'hole_id' => 'DH-001',
        ]);

        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/collars",
            [
                'hole_id' => 'DH-001',
                'easting' => 425000.5,
                'northing' => 6790000.0,
                'total_depth' => 350.0,
                'hole_type' => 'RC',
                'status' => 'Active',
            ],
        );

        $response->assertCreated();
    }

    public function test_store_returns_422_when_dip_out_of_range(): void
    {
        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/collars",
            [
                'hole_id' => 'DH-002',
                'easting' => 425000.5,
                'northing' => 6790000.0,
                'total_depth' => 100.0,
                'hole_type' => 'RC',
                'dip' => 95.0, // past vertical — invalid, must be -90 to 90
            ],
        );

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['dip']);
    }

    /**
     * §04e (SME-approved, Kyle, 2026-09-29): an up-hole is stored as measured.
     */
    public function test_store_accepts_an_up_hole_dip(): void
    {
        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/collars",
            [
                'hole_id' => 'UG-UP-01',
                'easting' => 425000.5,
                'northing' => 6790000.0,
                'total_depth' => 40.0,
                'hole_type' => 'Diamond',
                'azimuth' => 10.0,
                'dip' => 45.0,
                'status' => 'Active',
            ],
        );

        $response->assertCreated();
        $this->assertSame(
            45.0,
            (float) DB::table('silver.collars')->where('hole_id', 'UG-UP-01')->value('dip'),
        );
    }

    /**
     * §04e (SME-approved, Kyle, 2026-09-29): total depth is optional and is
     * stored as NULL, never 0.
     */
    public function test_store_accepts_a_collar_without_total_depth(): void
    {
        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/collars",
            [
                'hole_id' => 'NO-EOH-01',
                'easting' => 425000.5,
                'northing' => 6790000.0,
                'hole_type' => 'RC',
                'status' => 'Active',
            ],
        );

        $response->assertCreated()->assertJsonPath('data.total_depth', null);
        $this->assertNull(
            DB::table('silver.collars')->where('hole_id', 'NO-EOH-01')->value('total_depth'),
        );
    }

    public function test_store_rejects_a_zero_total_depth(): void
    {
        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/collars",
            [
                'hole_id' => 'ZERO-01',
                'easting' => 425000.5,
                'northing' => 6790000.0,
                'total_depth' => 0,
                'hole_type' => 'RC',
            ],
        );

        $response->assertUnprocessable()->assertJsonValidationErrors(['total_depth']);
    }

    // -------------------------------------------------------------------------
    // show
    // -------------------------------------------------------------------------

    public function test_show_returns_collar_with_all_relationships(): void
    {
        $collar = Collar::factory()->create([
            'project_id' => $this->project->project_id,
        ]);
        Survey::factory()->count(2)->create(['collar_id' => $collar->collar_id]);

        $response = $this->getJson(
            "/api/v1/projects/{$this->project->project_id}/collars/{$collar->collar_id}",
        );

        $response->assertOk()
            ->assertJsonPath('data.collar_id', $collar->collar_id)
            ->assertJsonStructure([
                'data' => [
                    'collar_id',
                    'surveys',
                    'lithology_logs',
                    'alterations',
                    'structures',
                    'samples',
                    'geochemistry',
                ],
            ]);
    }

    public function test_show_survives_a_survey_method_outside_the_vocabulary(): void
    {
        // THE BUG THIS PINS. §04e's SurveyMethod is a closed vocabulary of
        // three instrument families, but the ingestion writes provenance into
        // that column: `_SURVEY_METHOD_DEFAULT = 'unknown'` for any sheet that
        // names no instrument, and 'desurveyed_trace' for a Discover trace.
        // Cast straight to the enum, reading such a row threw a ValueError,
        // and CollarController::show catches Throwable — so ONE ingested
        // survey row answered 500 for every collar in the project.
        //
        // Inserted raw rather than through the factory because that is how it
        // actually happens: the ingestion writes to Postgres from Python and
        // never passes through Eloquent, so the `set` cast that would reject
        // this value is not in the path. Going through the factory here would
        // test the guard instead of the bug.
        $collar = Collar::factory()->create([
            'project_id' => $this->project->project_id,
        ]);

        foreach (['unknown', 'desurveyed_trace'] as $i => $method) {
            DB::table(Survey::getModel()->getTable())->insert([
                'survey_id' => (string) Str::uuid(),
                'collar_id' => $collar->collar_id,
                'depth' => 10.0 * ($i + 1),
                'azimuth' => 322.8,
                'dip' => 0.0,
                'survey_method' => $method,
            ]);
        }

        $response = $this->getJson(
            "/api/v1/projects/{$this->project->project_id}/collars/{$collar->collar_id}",
        );

        $response->assertOk();

        // Degrading must not lose what the file said. The payload stays a
        // string and still carries the stored value, so a geologist looking
        // at a trench sees 'desurveyed_trace' rather than a blank.
        $methods = array_column($response->json('data.surveys'), 'survey_method');
        sort($methods);
        $this->assertSame(['desurveyed_trace', 'unknown'], $methods);
    }

    public function test_show_returns_404_for_collar_in_wrong_project(): void
    {
        $other = Project::factory()->create();
        $collar = Collar::factory()->create(['project_id' => $other->project_id]);

        $response = $this->getJson(
            "/api/v1/projects/{$this->project->project_id}/collars/{$collar->collar_id}",
        );

        $response->assertNotFound();
    }

    // -------------------------------------------------------------------------
    // destroy
    // -------------------------------------------------------------------------

    public function test_destroy_deletes_collar_and_returns_204(): void
    {
        $collar = Collar::factory()->create([
            'project_id' => $this->project->project_id,
        ]);

        $response = $this->deleteJson(
            "/api/v1/projects/{$this->project->project_id}/collars/{$collar->collar_id}",
        );

        $response->assertNoContent();
        $this->assertDatabaseMissing('collars', ['collar_id' => $collar->collar_id]);
    }

    public function test_destroy_returns_404_for_nonexistent_collar(): void
    {
        $response = $this->deleteJson(
            "/api/v1/projects/{$this->project->project_id}/collars/00000000-0000-0000-0000-000000000000",
        );

        $response->assertNotFound();
    }
}
