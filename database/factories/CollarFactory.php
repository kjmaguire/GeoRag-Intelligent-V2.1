<?php

declare(strict_types=1);

namespace Database\Factories;

use App\Models\Collar;
use App\Models\Project;
use Illuminate\Database\Eloquent\Factories\Factory;
use Illuminate\Support\Str;

/**
 * @extends Factory<Collar>
 */
class CollarFactory extends Factory
{
    protected $model = Collar::class;

    public function definition(): array
    {
        return [
            'collar_id' => (string) Str::uuid(),
            'hole_id' => 'DH-'.$this->faker->unique()->numberBetween(1000, 99999),
            'project_id' => Project::factory(),
            'easting' => $this->faker->randomFloat(2, 400000, 700000),
            'northing' => $this->faker->randomFloat(2, 5000000, 7000000),
            'elevation' => $this->faker->randomFloat(2, 100, 2500),
            'total_depth' => $this->faker->randomFloat(2, 50, 1500),
            'hole_type' => $this->faker->randomElement(['Diamond', 'RC', 'Rotary', 'Auger']),
            'azimuth' => $this->faker->randomFloat(2, 0, 360),
            'dip' => $this->faker->randomFloat(2, -90, 0),
            'drill_date' => $this->faker->dateTimeBetween('-5 years', 'now')->format('Y-m-d'),
            'status' => $this->faker->randomElement(['Active', 'Completed', 'Abandoned']),
        ];
    }
}
