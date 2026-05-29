<?php

declare(strict_types=1);

namespace Database\Factories;

use App\Enums\ProjectStatus;
use App\Models\Project;
use Illuminate\Database\Eloquent\Factories\Factory;
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
 * @extends Factory<Project>
 */
class ProjectFactory extends Factory
{
    protected $model = Project::class;

    public function definition(): array
    {
        $projectName = $this->faker->unique()->company() . ' '
            . $this->faker->randomElement(['Project', 'Claim Group', 'Property']);

        return [
            'project_id'            => (string) Str::uuid(),
            'project_name'          => $projectName,
            'crs_datum'             => 'EPSG:32613',
            'company'               => $this->faker->company(),
            'magnetic_declination'  => $this->faker->randomFloat(2, -30, 30),
            'orientation_reference' => $this->faker->randomElement(['grid', 'true']),
            'commodity'             => $this->faker->randomElement([
                'Au', 'Ag', 'Cu', 'U3O8', 'Zn', 'Pb', 'Ni',
            ]),
            'region'                => $this->faker->randomElement([
                'Saskatchewan', 'British Columbia', 'Ontario', 'Québec', 'Nunavut',
            ]),
            'status'                => ProjectStatus::Active,
            'slug'                  => Str::slug($projectName) . '-' . $this->faker->unique()->numberBetween(1000, 9999),
        ];
    }

    /**
     * State: archived project.
     */
    public function archived(): static
    {
        return $this->state(fn () => ['status' => ProjectStatus::Archived]);
    }
}
