<?php

namespace Tests\Feature\Api\V1;

use App\Models\Collar;
use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\TestCase;

/**
 * Feature tests for ProjectController.
 *
 * The test suite uses SQLite in-memory (see phpunit.xml). Because the models
 * reference the 'silver' schema prefix and SQLite doesn't support schemas,
 * each test configures the model table names via a shared helper that strips
 * the schema prefix when running under SQLite.
 *
 * Note: tests assert HTTP contracts (status codes, response shape) — they do
 * NOT test geological domain logic, which lives in FastAPI.
 *
 * IMPORTANT: After the A2-01 IDOR fix, show/update/destroy require the
 * authenticated user to have a pivot row in project_user for the target
 * project. Tests that exercise these methods on a project the user owns
 * must call $this->user->projects()->attach(...) so the gate passes.
 */
class ProjectControllerTest extends TestCase
{
    use RefreshDatabase;

    private User $user;

    protected function setUp(): void
    {
        parent::setUp();

        // Strip the schema prefix so SQLite can find the table.
        // In a real Postgres test environment this override is unnecessary.
        Project::getModel()->setTable('projects');
        Collar::getModel()->setTable('collars');

        $this->user = User::factory()->create();
        $this->actingAs($this->user);
    }

    // -------------------------------------------------------------------------
    // index
    // -------------------------------------------------------------------------

    public function test_index_returns_paginated_projects(): void
    {
        $projects = Project::factory()->count(3)->create();
        foreach ($projects as $project) {
            $this->user->projects()->attach($project->project_id, ['role' => 'owner']);
        }

        $response = $this->getJson('/api/v1/projects');

        $response->assertOk()
            ->assertJsonStructure([
                'data' => [
                    '*' => [
                        'project_id',
                        'project_name',
                        'collar_count',
                        'created_at',
                        'updated_at',
                    ],
                ],
                'meta' => ['current_page', 'total'],
            ]);
    }

    public function test_index_returns_empty_list_when_no_projects_exist(): void
    {
        $response = $this->getJson('/api/v1/projects');

        $response->assertOk()
            ->assertJson(['data' => []]);
    }

    // -------------------------------------------------------------------------
    // store
    // -------------------------------------------------------------------------
    //
    // Creating a project requires a tenant to create it IN. A brand-new
    // account has no project memberships, so it has no workspace, so it
    // cannot create anything — which is the point: registration used to be
    // open, and store() used to fall back to a hardcoded workspace UUID, so
    // a stranger's second API call put them inside the production tenant.
    // An administrator bootstrapping a fresh deployment is the exception.

    /** Acting user who is allowed to create projects (fresh deployment). */
    private function actingAsAdmin(): User
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin);

        return $admin;
    }

    public function test_store_is_forbidden_for_a_user_with_no_workspace(): void
    {
        // $this->user from setUp() has no project memberships.
        $response = $this->postJson('/api/v1/projects', [
            'project_name' => 'Stranger Danger',
        ]);

        $response->assertForbidden();
        $this->assertDatabaseMissing('projects', ['project_name' => 'Stranger Danger']);
    }

    public function test_store_is_forbidden_for_a_read_only_viewer(): void
    {
        // The creator becomes the new project's owner, and project ownership
        // is what the audit-ledger and usage endpoints treat as workspace
        // administration. A viewer must not be able to promote themselves.
        $existing = Project::factory()->create([
            'workspace_id' => 'b0000000-0000-0000-0000-0000000000ff',
        ]);
        $this->user->projects()->attach($existing->project_id, ['role' => 'viewer']);

        $this->postJson('/api/v1/projects', [
            'project_name' => 'Viewer Promotion',
            'orientation_reference' => 'BOH',
        ])->assertForbidden();

        $this->assertDatabaseMissing('projects', ['project_name' => 'Viewer Promotion']);
    }

    public function test_an_admin_who_belongs_to_a_workspace_cannot_create_in_another_tenant(): void
    {
        $admin = $this->actingAsAdmin();
        $mine = Project::factory()->create([
            'workspace_id' => 'b0000000-0000-0000-0000-0000000000ff',
        ]);
        $admin->projects()->attach($mine->project_id, ['role' => 'owner']);

        // Creating a project would make the admin a member of the named
        // tenant, which a global admin flag must not reach on its own.
        $this->postJson('/api/v1/projects', [
            'project_name' => 'Admin Tenant Hop',
            'orientation_reference' => 'BOH',
            'workspace_id' => 'c0000000-0000-0000-0000-0000000000ff',
        ])->assertUnprocessable();

        $this->assertDatabaseMissing('projects', ['project_name' => 'Admin Tenant Hop']);
    }

    public function test_an_admin_with_no_workspace_may_bootstrap_a_named_one(): void
    {
        $this->actingAsAdmin();

        $this->postJson('/api/v1/projects', [
            'project_name' => 'First Project',
            'orientation_reference' => 'BOH',
            'workspace_id' => 'c0000000-0000-0000-0000-0000000000ff',
        ])->assertCreated();

        $this->assertDatabaseHas('projects', [
            'project_name' => 'First Project',
            'workspace_id' => 'c0000000-0000-0000-0000-0000000000ff',
        ]);
    }

    public function test_store_uses_the_creators_own_workspace(): void
    {
        $existing = Project::factory()->create([
            'workspace_id' => 'b0000000-0000-0000-0000-0000000000ff',
        ]);
        $this->user->projects()->attach($existing->project_id, ['role' => 'owner']);

        $response = $this->postJson('/api/v1/projects', [
            'project_name' => 'Second Property',
            'orientation_reference' => 'BOH',
        ]);

        $response->assertCreated();
        $this->assertDatabaseHas('projects', [
            'project_name' => 'Second Property',
            'workspace_id' => 'b0000000-0000-0000-0000-0000000000ff',
        ]);
    }

    public function test_store_refuses_a_workspace_the_creator_does_not_belong_to(): void
    {
        $mine = Project::factory()->create([
            'workspace_id' => 'b0000000-0000-0000-0000-0000000000ff',
        ]);
        $this->user->projects()->attach($mine->project_id, ['role' => 'owner']);

        $response = $this->postJson('/api/v1/projects', [
            'project_name' => 'Somebody Elses Tenant',
            'workspace_id' => 'a0000000-0000-0000-0000-000000000001',
        ]);

        $response->assertUnprocessable();
        $this->assertDatabaseMissing('projects', ['project_name' => 'Somebody Elses Tenant']);
    }

    public function test_store_is_ambiguous_when_the_creator_belongs_to_several_workspaces(): void
    {
        foreach (['b0000000-0000-0000-0000-0000000000ff', 'c0000000-0000-0000-0000-0000000000ff'] as $ws) {
            $p = Project::factory()->create(['workspace_id' => $ws]);
            $this->user->projects()->attach($p->project_id, ['role' => 'owner']);
        }

        // Picking one would put half this user's work in the wrong tenant.
        $this->postJson('/api/v1/projects', [
            'project_name' => 'Ambiguous',
            'orientation_reference' => 'BOH',
        ])->assertUnprocessable();

        $this->postJson('/api/v1/projects', [
            'project_name' => 'Ambiguous',
            'orientation_reference' => 'BOH',
            'workspace_id' => 'c0000000-0000-0000-0000-0000000000ff',
        ])->assertCreated();
    }

    public function test_store_creates_project_and_returns_201(): void
    {
        $this->actingAsAdmin();

        $payload = [
            'project_name' => 'Goldfields North',
            'crs_datum' => 'EPSG:32654',
            'company' => 'Apex Mining',
            'commodity' => 'Gold',
            'region' => 'Western Australia',
            'magnetic_declination' => -2.5,
            'orientation_reference' => 'BOH',
        ];

        $response = $this->postJson('/api/v1/projects', $payload);

        $response->assertCreated()
            ->assertJsonPath('data.project_name', 'Goldfields North')
            ->assertJsonPath('data.collar_count', 0);

        $this->assertDatabaseHas('projects', ['project_name' => 'Goldfields North']);
    }

    public function test_store_leaves_no_orphan_project_when_the_owner_row_cannot_be_written(): void
    {
        // save() then attach(owner) were two separate writes: when the second
        // failed, a project existed that nobody could see (membership is the
        // access rule) and the 500 left it behind. Both writes are one
        // transaction now.
        $this->actingAsAdmin();

        DB::listen(function ($query): void {
            if (str_contains($query->sql, 'insert into "project_user"')) {
                throw new \RuntimeException('simulated failure after the owner row was inserted');
            }
        });

        $this->postJson('/api/v1/projects', [
            'project_name' => 'Orphan Candidate',
            'orientation_reference' => 'BOH',
        ])->assertStatus(500);

        $this->assertDatabaseMissing('projects', ['project_name' => 'Orphan Candidate']);
    }

    public function test_store_returns_422_when_project_name_is_missing(): void
    {
        $this->actingAsAdmin();

        $response = $this->postJson('/api/v1/projects', [
            'company' => 'Apex Mining',
        ]);

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['project_name']);
    }

    public function test_store_returns_422_when_magnetic_declination_is_out_of_range(): void
    {
        $this->actingAsAdmin();

        $response = $this->postJson('/api/v1/projects', [
            'project_name' => 'Test Project',
            'magnetic_declination' => 999,
        ]);

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['magnetic_declination']);
    }

    public function test_store_returns_422_when_orientation_reference_is_invalid(): void
    {
        $this->actingAsAdmin();

        $response = $this->postJson('/api/v1/projects', [
            'project_name' => 'Test Project',
            'orientation_reference' => 'INVALID',
        ]);

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['orientation_reference']);
    }

    /**
     * Database audit 2026-09-29 PG-14: the column is NOT NULL, the rule was
     * nullable, so omitting it was a 500 on INSERT. It now defaults to BOH.
     */
    public function test_store_defaults_orientation_reference_to_boh_when_omitted(): void
    {
        $this->actingAsAdmin();

        $this->postJson('/api/v1/projects', ['project_name' => 'No Orientation Given'])
            ->assertCreated()
            ->assertJsonPath('data.orientation_reference', 'BOH');

        $this->postJson('/api/v1/projects', [
            'project_name' => 'Null Orientation Given',
            'orientation_reference' => null,
        ])->assertCreated()->assertJsonPath('data.orientation_reference', 'BOH');

        $this->postJson('/api/v1/projects', [
            'project_name' => 'Top Of Hole',
            'orientation_reference' => 'TOH',
        ])->assertCreated()->assertJsonPath('data.orientation_reference', 'TOH');
    }

    public function test_update_rejects_null_orientation_reference_instead_of_500(): void
    {
        $project = Project::factory()->create(['orientation_reference' => 'TOH']);
        $this->user->projects()->attach($project->project_id, ['role' => 'owner']);

        $this->patchJson("/api/v1/projects/{$project->project_id}", ['orientation_reference' => null])
            ->assertUnprocessable()
            ->assertJsonValidationErrors(['orientation_reference']);

        $this->patchJson("/api/v1/projects/{$project->project_id}", ['project_name' => 'Kept Orientation'])
            ->assertOk()
            ->assertJsonPath('data.orientation_reference', 'TOH');
    }

    // -------------------------------------------------------------------------
    // Azimuth north reference (Kyle, 2026-09-29): true / magnetic / grid join
    // BOH / TOH, and magnetic needs a declination (degrees, east positive).
    // -------------------------------------------------------------------------

    /**
     * @return array<string, array{0: string, 1: float|null}>
     */
    public static function azimuthReferences(): array
    {
        return [
            'grid north' => ['grid', null],
            'true north' => ['true', null],
            'magnetic north, east declination' => ['magnetic', 14.5],
            'magnetic north, west declination' => ['magnetic', -17.0],
        ];
    }

    #[DataProvider('azimuthReferences')]
    public function test_store_accepts_an_azimuth_north_reference(string $reference, ?float $declination): void
    {
        $this->actingAsAdmin();

        $payload = ['project_name' => "North {$reference}", 'orientation_reference' => $reference];
        if ($declination !== null) {
            $payload['magnetic_declination'] = $declination;
        }

        $this->postJson('/api/v1/projects', $payload)
            ->assertCreated()
            ->assertJsonPath('data.orientation_reference', $reference)
            // Loose: JSON renders -17.0 as -17.
            ->assertJson(['data' => ['magnetic_declination' => $declination]]);
    }

    public function test_store_requires_a_declination_for_magnetic_north(): void
    {
        $this->actingAsAdmin();

        $this->postJson('/api/v1/projects', [
            'project_name' => 'Magnetic Without Declination',
            'orientation_reference' => 'magnetic',
        ])->assertUnprocessable()
            ->assertJsonValidationErrors(['magnetic_declination']);

        $this->assertDatabaseMissing('projects', ['project_name' => 'Magnetic Without Declination']);
    }

    public function test_update_sets_magnetic_north_with_a_west_declination(): void
    {
        $project = Project::factory()->create(['orientation_reference' => 'BOH', 'magnetic_declination' => null]);
        $this->user->projects()->attach($project->project_id, ['role' => 'owner']);

        $this->patchJson("/api/v1/projects/{$project->project_id}", [
            'orientation_reference' => 'magnetic',
            'magnetic_declination' => '-17',
        ])->assertOk()
            ->assertJsonPath('data.orientation_reference', 'magnetic')
            ->assertJsonPath('data.magnetic_declination', -17);

        $project->refresh();
        $this->assertSame('magnetic', $project->orientation_reference);
        $this->assertSame(-17.0, $project->magnetic_declination);
    }

    public function test_update_refuses_magnetic_north_when_no_declination_is_stored_or_sent(): void
    {
        $project = Project::factory()->create(['orientation_reference' => 'BOH', 'magnetic_declination' => null]);
        $this->user->projects()->attach($project->project_id, ['role' => 'owner']);

        $this->patchJson("/api/v1/projects/{$project->project_id}", ['orientation_reference' => 'magnetic'])
            ->assertUnprocessable()
            ->assertJsonValidationErrors(['magnetic_declination']);

        $this->assertSame('BOH', $project->refresh()->orientation_reference);
    }

    public function test_update_to_magnetic_north_keeps_an_already_stored_declination(): void
    {
        $project = Project::factory()->create(['orientation_reference' => 'true', 'magnetic_declination' => 12.25]);
        $this->user->projects()->attach($project->project_id, ['role' => 'owner']);

        $this->patchJson("/api/v1/projects/{$project->project_id}", ['orientation_reference' => 'magnetic'])
            ->assertOk()
            ->assertJsonPath('data.magnetic_declination', 12.25);
    }

    public function test_update_refuses_clearing_the_declination_of_a_magnetic_project(): void
    {
        $project = Project::factory()->create(['orientation_reference' => 'magnetic', 'magnetic_declination' => 8.0]);
        $this->user->projects()->attach($project->project_id, ['role' => 'owner']);

        // The sheet's emptied box arrives as '' — "not recorded", never 0.
        $this->patchJson("/api/v1/projects/{$project->project_id}", ['magnetic_declination' => ''])
            ->assertUnprocessable()
            ->assertJsonValidationErrors(['magnetic_declination']);

        $this->assertSame(8.0, $project->refresh()->magnetic_declination);
    }

    public function test_update_clears_the_declination_to_null_not_zero(): void
    {
        $project = Project::factory()->create(['orientation_reference' => 'true', 'magnetic_declination' => 8.0]);
        $this->user->projects()->attach($project->project_id, ['role' => 'owner']);

        $this->patchJson("/api/v1/projects/{$project->project_id}", ['magnetic_declination' => ''])
            ->assertOk()
            ->assertJsonPath('data.magnetic_declination', null);

        $this->assertNull($project->refresh()->magnetic_declination);
    }

    public function test_update_magnetic_check_runs_after_the_membership_gate(): void
    {
        // Not a member: the answer is the usual 404, not a declination error
        // that would reveal anything about another tenant's project.
        $project = Project::factory()->create(['orientation_reference' => 'BOH', 'magnetic_declination' => null]);

        $this->patchJson("/api/v1/projects/{$project->project_id}", ['orientation_reference' => 'magnetic'])
            ->assertNotFound();
    }

    // -------------------------------------------------------------------------
    // show
    // -------------------------------------------------------------------------

    public function test_show_returns_project_with_collar_count(): void
    {
        $project = Project::factory()->create(['project_name' => 'Show Test Project']);
        // Attach user so the hasProjectAccess gate passes (A2-01 fix).
        $this->user->projects()->attach($project->project_id, ['role' => 'owner']);
        Collar::factory()->count(4)->create(['project_id' => $project->project_id]);

        $response = $this->getJson("/api/v1/projects/{$project->project_id}");

        $response->assertOk()
            ->assertJsonPath('data.project_id', $project->project_id)
            ->assertJsonPath('data.collar_count', 4);
    }

    public function test_show_returns_404_for_nonexistent_project(): void
    {
        $response = $this->getJson('/api/v1/projects/00000000-0000-0000-0000-000000000000');

        $response->assertNotFound();
    }

    // -------------------------------------------------------------------------
    // update
    // -------------------------------------------------------------------------

    public function test_update_modifies_project_and_returns_200(): void
    {
        $project = Project::factory()->create(['project_name' => 'Original Name']);
        // Attach user so the hasProjectAccess gate passes (A2-01 fix).
        $this->user->projects()->attach($project->project_id, ['role' => 'owner']);

        $response = $this->patchJson("/api/v1/projects/{$project->project_id}", [
            'project_name' => 'Renamed Project',
        ]);

        $response->assertOk()
            ->assertJsonPath('data.project_name', 'Renamed Project');

        $this->assertDatabaseHas('projects', ['project_name' => 'Renamed Project']);
    }

    public function test_update_returns_404_for_nonexistent_project(): void
    {
        $response = $this->patchJson('/api/v1/projects/00000000-0000-0000-0000-000000000000', [
            'project_name' => 'Ghost Project',
        ]);

        $response->assertNotFound();
    }

    // -------------------------------------------------------------------------
    // destroy
    // -------------------------------------------------------------------------

    public function test_destroy_deletes_project_and_returns_204(): void
    {
        $project = Project::factory()->create();
        // Attach user so the hasProjectAccess gate passes (A2-01 fix).
        $this->user->projects()->attach($project->project_id, ['role' => 'owner']);

        $response = $this->deleteJson("/api/v1/projects/{$project->project_id}");

        $response->assertNoContent();
        $this->assertDatabaseMissing('projects', ['project_id' => $project->project_id]);
    }

    // -------------------------------------------------------------------------
    // SEC-5 — only the owner or an admin may edit or delete a whole project
    // -------------------------------------------------------------------------

    /**
     * @return array<string, array{0: string}>
     */
    public static function nonOwnerRoles(): array
    {
        return ['member' => ['member'], 'viewer' => ['viewer']];
    }

    #[DataProvider('nonOwnerRoles')]
    public function test_a_non_owner_member_cannot_delete_the_project(string $role): void
    {
        $project = Project::factory()->create();
        $this->user->projects()->attach($project->project_id, ['role' => $role]);

        $this->deleteJson("/api/v1/projects/{$project->project_id}")
            ->assertForbidden();

        $this->assertDatabaseHas('projects', ['project_id' => $project->project_id]);
    }

    #[DataProvider('nonOwnerRoles')]
    public function test_a_non_owner_member_cannot_edit_the_project(string $role): void
    {
        $project = Project::factory()->create(['project_name' => 'Original Name']);
        $this->user->projects()->attach($project->project_id, ['role' => $role]);

        $this->patchJson("/api/v1/projects/{$project->project_id}", [
            'project_name' => 'Hijacked',
        ])->assertForbidden();

        $this->assertDatabaseHas('projects', [
            'project_id' => $project->project_id,
            'project_name' => 'Original Name',
        ]);
    }

    public function test_an_admin_member_can_delete_a_project_they_do_not_own(): void
    {
        $this->user->forceFill(['is_admin' => true])->save();
        $project = Project::factory()->create();
        $this->user->projects()->attach($project->project_id, ['role' => 'member']);

        $this->deleteJson("/api/v1/projects/{$project->project_id}")
            ->assertNoContent();

        $this->assertDatabaseMissing('projects', ['project_id' => $project->project_id]);
    }

    public function test_an_admin_who_is_not_a_member_still_gets_404(): void
    {
        // Admin widens what a MEMBER may do; it does not turn the existence
        // oracle defence into a 403 that confirms the project exists.
        $this->user->forceFill(['is_admin' => true])->save();
        $project = Project::factory()->create();

        $this->deleteJson("/api/v1/projects/{$project->project_id}")
            ->assertNotFound();

        $this->assertDatabaseHas('projects', ['project_id' => $project->project_id]);
    }

    public function test_destroy_returns_404_for_nonexistent_project(): void
    {
        $response = $this->deleteJson('/api/v1/projects/00000000-0000-0000-0000-000000000000');

        $response->assertNotFound();
    }

    /**
     * Regression for 2026-08-17: silver.mineral_claims does not exist in the
     * live (canadacentral) database at all — it was only ever created by an
     * out-of-band raw-SQL bootstrap script, never a tracked migration, and
     * the freshly-provisioned Azure Postgres server never got it. destroy()
     * unconditionally ran `DELETE FROM silver.mineral_claims`, which threw a
     * "relation does not exist" error on every call, rolling back the whole
     * transaction — project deletion was 100% broken for every project.
     *
     * The test-DB parity migration (2026_06_29_020000_provision_project_
     * delete_tables_for_test_db.php) always stubs a dummy mineral_claims
     * table, which is why this bug was invisible to the SQLite suite until
     * now — dropping that stub table here reproduces the exact live gap.
     */
    public function test_destroy_succeeds_when_a_listed_cleanup_table_does_not_exist(): void
    {
        Schema::dropIfExists('mineral_claims');

        $project = Project::factory()->create();
        $this->user->projects()->attach($project->project_id, ['role' => 'owner']);

        $response = $this->deleteJson("/api/v1/projects/{$project->project_id}");

        $response->assertNoContent();
        $this->assertDatabaseMissing('projects', ['project_id' => $project->project_id]);
    }
}
