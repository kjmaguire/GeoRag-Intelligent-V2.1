<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * The New Project wizard's "Project code" used to be collected and then
 * dropped: neither the request nor the controller knew the field, so the
 * code the geologist typed was never stored. silver.projects.project_code
 * (Postgres-only) is unique per workspace.
 */
final class ProjectCodeTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private User $user;

    protected function setUp(): void
    {
        parent::setUp();

        $this->user = User::factory()->create();
        $existing = Project::factory()->create();
        $this->user->projects()->attach($existing->project_id, ['role' => 'owner']);
        $this->actingAs($this->user);
    }

    public function test_the_project_code_is_stored_and_returned(): void
    {
        $this->postJson('/api/v1/projects', [
            'project_name' => 'Coded Property',
            'orientation_reference' => 'BOH',
            'project_code' => 'EGL-01',
        ])->assertCreated()->assertJsonPath('data.project_code', 'EGL-01');

        $this->assertDatabaseHas('silver.projects', [
            'project_name' => 'Coded Property',
            'project_code' => 'EGL-01',
        ]);
    }

    public function test_a_code_already_used_in_the_workspace_is_refused(): void
    {
        $payload = [
            'project_name' => 'First',
            'orientation_reference' => 'BOH',
            'project_code' => 'EGL-02',
        ];
        $this->postJson('/api/v1/projects', $payload)->assertCreated();

        $this->postJson('/api/v1/projects', [...$payload, 'project_name' => 'Second'])
            ->assertUnprocessable()
            ->assertJsonValidationErrors(['project_code']);
    }
}
