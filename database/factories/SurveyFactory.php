<?php

declare(strict_types=1);

namespace Database\Factories;

use App\Models\Collar;
use App\Models\Survey;
use Illuminate\Database\Eloquent\Factories\Factory;
use Illuminate\Support\Str;

/**
 * @extends Factory<Survey>
 */
class SurveyFactory extends Factory
{
    protected $model = Survey::class;

    public function definition(): array
    {
        return [
            'survey_id'     => (string) Str::uuid(),
            'collar_id'     => Collar::factory(),
            'depth'         => $this->faker->randomFloat(2, 0, 1500),
            'azimuth'       => $this->faker->randomFloat(2, 0, 360),
            'dip'           => $this->faker->randomFloat(2, -90, 0),
            'survey_method' => $this->faker->randomElement(['Gyro', 'Magnetic', 'Multishot']),
        ];
    }
}
