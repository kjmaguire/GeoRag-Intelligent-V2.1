<?php

declare(strict_types=1);

namespace Tests\Unit\Services\Exports;

use App\Services\Exports\CommodityAssayValue;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\TestCase;

/**
 * assays.csv of the CSA bundle read the keys `u3o8_ppm`, `au_ppb`, `cu_pct`.
 * The ingestion writes canonical-case keys in normalised units (`U3O8_ppm`,
 * `Au_ppm`, `Cu_pct`; _assay_columns.py), so those lookups never matched.
 */
final class CommodityAssayValueTest extends TestCase
{
    public function test_gold_stored_in_ppm_reads_as_ppb(): void
    {
        // The audit's case.
        $this->assertSame('1200', CommodityAssayValue::in(['Au_ppm' => 1.2], 'Au', 'ppb'));
    }

    public function test_a_key_already_in_the_wanted_unit_is_returned_untouched(): void
    {
        $this->assertSame(1250, CommodityAssayValue::in(['U3O8_ppm' => 1250], 'U3O8', 'ppm'));
        $this->assertSame(0.35, CommodityAssayValue::in(['Cu_pct' => 0.35], 'Cu', 'pct'));
        $this->assertSame(45, CommodityAssayValue::in(['Au_ppb' => 45, 'Au_ppm' => 99], 'Au', 'ppb'), 'the exact unit wins over a conversion');
    }

    /**
     * @return iterable<string, array{array<string, mixed>, string, string, string}>
     */
    public static function conversions(): iterable
    {
        yield 'ppm to ppb' => [['Au_ppm' => 1.2], 'Au', 'ppb', '1200'];
        yield 'ppm to pct' => [['Cu_ppm' => 3500], 'Cu', 'pct', '0.35'];
        yield 'pct to ppm' => [['U3O8_pct' => 0.125], 'U3O8', 'ppm', '1250'];
        yield 'ppb to ppm' => [['Au_ppb' => 450], 'Au', 'ppm', '0.45'];
        yield 'pct to ppb' => [['Au_pct' => 0.0001], 'Au', 'ppb', '1000'];
        yield 'ppb to pct' => [['Cu_ppb' => 3500000], 'Cu', 'pct', '0.35'];
        yield 'no float noise' => [['Au_ppm' => 0.07], 'Au', 'ppb', '70'];
        yield 'small value, no exponent' => [['Au_ppb' => 1], 'Au', 'pct', '0.0000001'];
        yield 'zero' => [['Au_ppm' => 0], 'Au', 'ppb', '0'];
    }

    /**
     * @param array<string, mixed> $assays
     */
    #[DataProvider('conversions')]
    public function test_units_are_converted(array $assays, string $element, string $unit, string $expected): void
    {
        $this->assertSame($expected, CommodityAssayValue::in($assays, $element, $unit));
    }

    public function test_the_element_is_matched_case_insensitively(): void
    {
        $this->assertSame(1250, CommodityAssayValue::in(['u3o8_ppm' => 1250], 'U3O8', 'ppm'));
        $this->assertSame(1250, CommodityAssayValue::in(['U3O8_PPM' => 1250], 'u3o8', 'ppm'));
        $this->assertSame('1200', CommodityAssayValue::in(['AU_ppm' => 1.2], 'Au', 'ppb'));
    }

    public function test_keys_that_are_not_a_chemical_assay_of_the_element_are_ignored(): void
    {
        $assays = [
            'U3O8_pct_e' => 9.9,    // radiometric equivalent
            'eU3O8_ppm' => 8.8,     // a different analyte
            'U_ppm' => 7.7,         // uranium, not the oxide
            'Au' => 6.6,            // no unit
            'Au_gpt' => 5.5,        // not a stored unit
            'n_points' => 4,
            'confidence' => 0.9,
        ];

        $this->assertNull(CommodityAssayValue::in($assays, 'U3O8', 'ppm'));
        $this->assertNull(CommodityAssayValue::in($assays, 'Au', 'ppb'));
    }

    public function test_a_value_that_is_not_a_number_is_blank_not_zero(): void
    {
        $this->assertNull(CommodityAssayValue::in(['Au_ppm' => '<0.005'], 'Au', 'ppb'));
        $this->assertNull(CommodityAssayValue::in(['Au_ppm' => null], 'Au', 'ppb'));
        $this->assertNull(CommodityAssayValue::in([], 'Au', 'ppb'));
        $this->assertSame('1200', CommodityAssayValue::in(['Au_ppm' => '1.2'], 'Au', 'ppb'), 'a numeric string is a number');
    }
}
