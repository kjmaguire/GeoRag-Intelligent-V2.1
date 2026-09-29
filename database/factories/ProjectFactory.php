<?php

declare(strict_types=1);

namespace Database\Factories;

use App\Enums\ProjectStatus;
use App\Models\Project;
use Illuminate\Database\Eloquent\Factories\Factory;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;

/**
 * Project factory — R1 follow-up.
 *
 * Added so feature tests (QueryAuditPiiEncryptionTest, QueryChannelAuthorizationTest,
 * etc.) can seed a project without hard-coding UUIDs. Fields match the
 * Project model's fillable set; defaults are deterministic enough for
 * tests but carry enough variation via Faker that parallel tests don't
 * collide on name/slug uniqueness.
 *
 * Every project gets a real silver.workspaces row of its own, because every
 * production project has one: BindWorkspaceRlsContext refuses a project with
 * a NULL workspace on Postgres (409 to a member, 404 to anyone else — SEC-10),
 * so a factory that left it NULL made every project-scoped route in the
 * Postgres suite test that refusal instead of the route. One workspace per
 * project, not a shared one, so two factory projects are two tenants — which
 * is what the cross-tenant IDOR tests assume. Pass `workspace_id` to reuse a
 * workspace (the closure below is then never called), or
 * `['workspace_id' => null]` to test the refusal itself.
 *
 * @extends Factory<Project>
 */
class ProjectFactory extends Factory
{
    protected $model = Project::class;

    public function definition(): array
    {
        $projectName = $this->faker->unique()->company().' '
            .$this->faker->randomElement(['Project', 'Claim Group', 'Property']);

        return [
            'project_id' => (string) Str::uuid(),
            'project_name' => $projectName,
            'crs_datum' => 'EPSG:32613',
            'company' => $this->faker->company(),
            'magnetic_declination' => $this->faker->randomFloat(2, -30, 30),
            'orientation_reference' => $this->faker->randomElement(['BOH', 'TOH']),
            'commodity' => $this->faker->randomElement([
                'Au', 'Ag', 'Cu', 'U3O8', 'Zn', 'Pb', 'Ni',
            ]),
            'region' => $this->faker->randomElement([
                'Saskatchewan', 'British Columbia', 'Ontario', 'Québec', 'Nunavut',
            ]),
            'status' => ProjectStatus::Active,
            'slug' => Str::slug($projectName).'-'.$this->faker->unique()->numberBetween(1000, 9999),
            'workspace_id' => fn (): string => $this->createWorkspace(),
        ];
    }

    /**
     * Insert a fresh silver.workspaces row and return its id.
     *
     * Through the query builder rather than a model: there is no Workspace
     * model, and the builder's `"silver".` prefix is what the SQLite suite's
     * schema-stripping hook in Tests\TestCase rewrites to its `workspaces`
     * mirror table.
     */
    private function createWorkspace(): string
    {
        $workspaceId = (string) Str::uuid();

        DB::table('silver.workspaces')->insert([
            'workspace_id' => $workspaceId,
            'name' => 'Factory Workspace '.substr($workspaceId, 0, 8),
            'slug' => 'factory-ws-'.$workspaceId,
            'data_version' => 0,
            'created_at' => now(),
            'updated_at' => now(),
        ]);

        return $workspaceId;
    }

    /**
     * State: archived project.
     */
    public function archived(): static
    {
        return $this->state(fn () => ['status' => ProjectStatus::Archived]);
    }
}
