<?php

namespace Tests\Feature\Api\V1;

use App\Models\Collar;
use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

/**
 * IDOR regression tests for CollarController (index / store / show / destroy).
 *
 * Verifies finding A2-02 from the 2026-04-22 security audit:
 *   "All four methods (index/store/show/destroy) leak collar records
 *    cross-tenant. The scoped() route binding doesn't enforce parent-pivot
 *    ownership — only that the child belongs to the parent UUID."
 *
 * Pattern: User A authenticates, then attempts collar operations against a
 * project owned exclusively by User B. All must return 404.
 *
 * SQLite note: CollarController uses ST_X / ST_Transform in selectRaw so
 * these tests must run against PostgreSQL. Tests that trigger those spatial
 * queries are skipped under SQLite. The pure membership-gate tests (which
 * short-circuit before any DB work) are safe under SQLite and will run.
 */
class CollarControllerIDORTest extends TestCase
{
    use RefreshDatabase;

    private User $userA;
    private User $userB;
    private Project $projectB;

    protected function setUp(): void
    {
        parent::setUp();

        Project::getModel()->setTable('projects');
        Collar::getModel()->setTable('collars');

        $this->userA = User::factory()->create();
        $this->userB = User::factory()->create();

        // projectB is owned by userB only — no pivot row for userA.
        $this->projectB = Project::factory()->create();
        $this->userB->projects()->attach($this->projectB->project_id, ['role' => 'owner']);

        $this->actingAs($this->userA, 'sanctum');
    }

    // -------------------------------------------------------------------------
    // IDOR: index — list collars of another user's project
    // -------------------------------------------------------------------------

    public function test_user_a_cannot_list_collars_of_user_b_project(): void
    {
        $response = $this->getJson(
            "/api/v1/projects/{$this->projectB->project_id}/collars"
        );

        // Gate fires before PostGIS query so this is safe under SQLite too.
        $response->assertNotFound()
            ->assertJsonPath('message', 'Project not found.');
    }

    // -------------------------------------------------------------------------
    // IDOR: store — create collar in another user's project
    // -------------------------------------------------------------------------

    public function test_user_a_cannot_create_collar_in_user_b_project(): void
    {
        $response = $this->postJson(
            "/api/v1/projects/{$this->projectB->project_id}/collars",
            [
                'hole_id'     => 'STOLEN-001',
                'easting'     => 425000.5,
                'northing'    => 6790000.0,
                'total_depth' => 100.0,
                'hole_type'   => 'RC',
                'status'      => 'Active',
            ]
        );

        // Gate fires before PostGIS / validation so this is safe under SQLite.
        $response->assertNotFound()
            ->assertJsonPath('message', 'Project not found.');
    }

    // -------------------------------------------------------------------------
    // IDOR: show — read a specific collar in another user's project
    // -------------------------------------------------------------------------

    public function test_user_a_cannot_read_collar_in_user_b_project(): void
    {
        $this->skipIfSqlite('show uses ST_X/ST_Transform — requires PostGIS.');

        $collar = Collar::factory()->create([
            'project_id' => $this->projectB->project_id,
        ]);

        $response = $this->getJson(
            "/api/v1/projects/{$this->projectB->project_id}/collars/{$collar->collar_id}"
        );

        $response->assertNotFound();
    }

    // -------------------------------------------------------------------------
    // IDOR: destroy — delete a collar in another user's project
    // -------------------------------------------------------------------------

    public function test_user_a_cannot_delete_collar_in_user_b_project(): void
    {
        $this->skipIfSqlite('destroy queries PostGIS-backed collar table.');

        $collar = Collar::factory()->create([
            'project_id' => $this->projectB->project_id,
        ]);

        $response = $this->deleteJson(
            "/api/v1/projects/{$this->projectB->project_id}/collars/{$collar->collar_id}"
        );

        $response->assertNotFound();

        // Confirm the collar was NOT deleted.
        $this->assertDatabaseHas('collars', ['collar_id' => $collar->collar_id]);
    }

    // -------------------------------------------------------------------------
    // Sanity: user A can list collars on their own project
    // -------------------------------------------------------------------------

    public function test_user_a_can_list_collars_on_own_project(): void
    {
        $this->skipIfSqlite('index uses ST_X/ST_Transform — requires PostGIS.');

        $projectA = Project::factory()->create();
        $this->userA->projects()->attach($projectA->project_id, ['role' => 'owner']);

        Collar::factory()->count(2)->create(['project_id' => $projectA->project_id]);

        $response = $this->getJson(
            "/api/v1/projects/{$projectA->project_id}/collars"
        );

        $response->assertOk();
        $this->assertCount(2, $response->json('data'));
    }
}
