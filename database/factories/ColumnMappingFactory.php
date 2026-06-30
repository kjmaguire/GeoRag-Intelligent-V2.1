<?php

declare(strict_types=1);

namespace Database\Factories;

use App\Models\ColumnMapping;
use App\Models\VendorProfile;
use Illuminate\Database\Eloquent\Factories\Factory;

/**
 * @extends Factory<ColumnMapping>
 */
class ColumnMappingFactory extends Factory
{
    protected $model = ColumnMapping::class;

    public function definition(): array
    {
        // Canonical field names are drawn from a realistic geological vocabulary
        // so test output is readable without being domain-specific.
        $canonicalFields = [
            'hole_id', 'from_depth', 'to_depth', 'azimuth', 'dip',
            'sample_id', 'au_ppm', 'cu_pct', 'rock_type', 'easting',
            'northing', 'elevation', 'lith_code', 'alteration', 'structure',
        ];

        return [
            'vendor_profile_id' => VendorProfile::factory(),
            'parser_type' => $this->faker->randomElement(ColumnMapping::PARSER_TYPES),
            'canonical_field' => $this->faker->unique()->randomElement($canonicalFields),
            'source_column' => $this->faker->word().'_'.$this->faker->numberBetween(1, 99),
            'source_unit' => $this->faker->optional()->randomElement(['m', 'ft', 'ppm', 'ppb', '%', 'g/t']),
            'target_unit' => $this->faker->optional()->randomElement(['m', 'ppm', 'g/t', '%']),
            'notes' => $this->faker->optional()->sentence(),
        ];
    }

    /**
     * State: mapping for a specific parser type.
     */
    public function forParserType(string $parserType): static
    {
        return $this->state(fn () => ['parser_type' => $parserType]);
    }
}
