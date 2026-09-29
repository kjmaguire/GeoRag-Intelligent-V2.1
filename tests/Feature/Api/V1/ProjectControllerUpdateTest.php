<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Events\Workspace\WorkspaceActivityBroadcast;
use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Event;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * PATCH /api/v1/projects/{project} — the endpoint behind the Overview's
 * "Edit project" sheet (resources/js/Components/EditProjectSheet.tsx).
 *
 * ProjectControllerTest covers the one-field rename and ProjectControllerIDORTest
 * the cross-user 404. This file covers what the sheet actually sends (all four
 * surfaced fields at once), clearing optional fields, the fields that must stay
 * immutable even when a client sends them, validation, and the tenancy gate
 * across workspaces — the same membership check destroy() uses.
 *
 * SQLite in-memory, like its siblings: the schema prefix is stripped in setUp.
 */
final class ProjectControllerUpdateTest extends TestCase
{
    use RefreshDatabase;

    private User $user;

    private string $workspaceId;

    protected function setUp(): void
    {
        parent::setUp();

        Project::getModel()->setTable('projects');

        $this->user = User::factory()->create();
        $this->workspaceId = (string) Str::uuid();
    }

    private function makeProject(User $owner, string $workspaceId, array $attributes = []): Project
    {
        $project = Project::factory()->create($attributes);
        DB::table('projects')
            ->where('project_id', $project->project_id)
            ->update(['workspace_id' => $workspaceId, 'crs_epsg' => 26913]);
        $owner->projects()->attach($project->project_id, ['role' => 'owner']);

        return $project->refresh();
    }

    public function test_member_can_edit_every_field_the_sheet_surfaces(): void
    {
        Event::fake([WorkspaceActivityBroadcast::class]);
        $project = $this->makeProject($this->user, $this->workspaceId, [
            'project_name' => 'Shirley Basin',
            'company' => 'Old Operator Ltd',
            'commodity' => 'uranium',
            'region' => 'WY',
        ]);

        $response = $this->actingAs($this->user)->patchJson("/api/v1/projects/{$project->project_id}", [
            'project_name' => 'Shirley Basin North',
            'company' => 'New Operator Inc',
            'commodity' => 'gold',
            'region' => 'NV',
        ]);

        $response->assertOk()
            ->assertJsonPath('data.project_name', 'Shirley Basin North')
            ->assertJsonPath('data.company', 'New Operator Inc')
            ->assertJsonPath('data.commodity', 'gold')
            ->assertJsonPath('data.region', 'NV')
            // A rename must not move the project's URL.
            ->assertJsonPath('data.slug', $project->slug);

        $this->assertDatabaseHas('projects', [
            'project_id' => $project->project_id,
            'project_name' => 'Shirley Basin North',
            'company' => 'New Operator Inc',
            'commodity' => 'gold',
            'region' => 'NV',
            'slug' => $project->slug,
        ]);

        Event::assertDispatched(
            WorkspaceActivityBroadcast::class,
            fn (WorkspaceActivityBroadcast $event): bool => $event->workspaceId === $this->workspaceId
                && $event->payload === ['verb' => 'updated', 'project_id' => $project->project_id],
        );
    }

    public function test_blank_optional_fields_are_cleared_to_null(): void
    {
        $project = $this->makeProject($this->user, $this->workspaceId, [
            'company' => 'Operator',
            'commodity' => 'copper',
            'region' => 'BC',
        ]);

        $this->actingAs($this->user)->patchJson("/api/v1/projects/{$project->project_id}", [
            'project_name' => $project->project_name,
            'company' => '',
            'commodity' => '',
            'region' => '',
        ])->assertOk();

        $this->assertDatabaseHas('projects', [
            'project_id' => $project->project_id,
            'company' => null,
            'commodity' => null,
            'region' => null,
        ]);
    }

    public function test_identity_tenancy_and_crs_fields_are_ignored_even_when_sent(): void
    {
        $project = $this->makeProject($this->user, $this->workspaceId);
        $otherWorkspace = (string) Str::uuid();

        $this->actingAs($this->user)->patchJson("/api/v1/projects/{$project->project_id}", [
            'project_name' => 'Renamed',
            'slug' => 'hijacked-slug',
            'workspace_id' => $otherWorkspace,
            'crs_epsg' => 32612,
            'status' => 'archived',
        ])->assertOk();

        $this->assertDatabaseHas('projects', [
            'project_id' => $project->project_id,
            'project_name' => 'Renamed',
            'slug' => $project->slug,
            'workspace_id' => $this->workspaceId,
            'crs_epsg' => 26913,
            'status' => 'active',
        ]);
    }

    public function test_blank_project_name_is_rejected(): void
    {
        $project = $this->makeProject($this->user, $this->workspaceId, ['project_name' => 'Keep Me']);

        $this->actingAs($this->user)
            ->patchJson("/api/v1/projects/{$project->project_id}", ['project_name' => '   '])
            ->assertUnprocessable()
            ->assertJsonValidationErrors(['project_name' => 'A project name is required.']);

        $this->assertDatabaseHas('projects', ['project_id' => $project->project_id, 'project_name' => 'Keep Me']);
    }

    public function test_overlong_values_are_rejected_per_column_width(): void
    {
        $project = $this->makeProject($this->user, $this->workspaceId, ['commodity' => 'gold']);

        $this->actingAs($this->user)
            ->patchJson("/api/v1/projects/{$project->project_id}", [
                'project_name' => str_repeat('a', 256),
                'commodity' => str_repeat('b', 51),
                'company' => str_repeat('c', 256),
                'region' => str_repeat('d', 256),
            ])
            ->assertUnprocessable()
            ->assertJsonValidationErrors(['project_name', 'commodity', 'company', 'region']);

        $this->assertDatabaseHas('projects', ['project_id' => $project->project_id, 'commodity' => 'gold']);
    }

    public function test_a_project_in_another_workspace_is_not_found_and_unchanged(): void
    {
        $this->makeProject($this->user, $this->workspaceId);

        $stranger = User::factory()->create();
        $theirs = $this->makeProject($stranger, (string) Str::uuid(), ['project_name' => 'Their Project']);

        $this->actingAs($this->user)
            ->patchJson("/api/v1/projects/{$theirs->project_id}", ['project_name' => 'Hijacked'])
            ->assertNotFound()
            ->assertJsonPath('message', 'Project not found.');

        $this->assertDatabaseHas('projects', ['project_id' => $theirs->project_id, 'project_name' => 'Their Project']);
    }

    public function test_a_non_member_in_the_same_workspace_is_not_found(): void
    {
        // Membership is per project (the project_user pivot), exactly as for
        // destroy() — sharing a workspace is not enough.
        $colleague = User::factory()->create();
        $project = $this->makeProject($colleague, $this->workspaceId, ['project_name' => 'Colleague Project']);
        $this->makeProject($this->user, $this->workspaceId);

        $this->actingAs($this->user)
            ->patchJson("/api/v1/projects/{$project->project_id}", ['project_name' => 'Hijacked'])
            ->assertNotFound();

        $this->assertDatabaseHas('projects', ['project_id' => $project->project_id, 'project_name' => 'Colleague Project']);
    }

    public function test_guests_are_rejected(): void
    {
        $project = $this->makeProject($this->user, $this->workspaceId, ['project_name' => 'Untouched']);

        $this->patchJson("/api/v1/projects/{$project->project_id}", ['project_name' => 'Anonymous'])
            ->assertUnauthorized();

        $this->assertDatabaseHas('projects', ['project_id' => $project->project_id, 'project_name' => 'Untouched']);
    }
}
