<?php

declare(strict_types=1);

namespace Database\Factories;

use App\Models\User;
use App\Models\VendorProfile;
use Illuminate\Database\Eloquent\Factories\Factory;

/**
 * @extends Factory<VendorProfile>
 */
class VendorProfileFactory extends Factory
{
    protected $model = VendorProfile::class;

    public function definition(): array
    {
        return [
            'name'                => $this->faker->unique()->company() . ' Profile',
            'description'         => $this->faker->optional()->sentence(),
            'profile_type'        => $this->faker->randomElement(VendorProfile::PROFILE_TYPES),
            'is_global'           => $this->faker->boolean(),
            'created_by_user_id'  => User::factory(),
        ];
    }

    /**
     * State: lab vendor profile.
     */
    public function lab(): static
    {
        return $this->state(fn () => ['profile_type' => 'lab']);
    }

    /**
     * State: driller vendor profile.
     */
    public function driller(): static
    {
        return $this->state(fn () => ['profile_type' => 'driller']);
    }

    /**
     * State: globally shared profile (visible to all users/projects).
     */
    public function global(): static
    {
        return $this->state(fn () => ['is_global' => true]);
    }

    /**
     * State: project-specific (non-global) profile.
     */
    public function notGlobal(): static
    {
        return $this->state(fn () => ['is_global' => false]);
    }
}
