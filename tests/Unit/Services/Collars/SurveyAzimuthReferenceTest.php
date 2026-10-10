<?php

declare(strict_types=1);

namespace Tests\Unit\Services\Collars;

use App\Services\Collars\SurveyAzimuthReference;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\TestCase;

/**
 * SurveyAzimuthReference converts a DECLARED azimuth reference to an azimuth
 * from true north for the Workspace 3D frame (GIS audit 2026-10).
 *
 * It is the PHP half of one rule the FastAPI promote step applies in Python
 * (app/services/ingest/azimuth_reference.py). The expected numbers below are
 * the SAME literals asserted in
 * src/fastapi/tests/test_azimuth_reference_php_parity.py, where they come out
 * of the Python module: edit one side and the other test fails.
 *
 * Fixture: a collar at 58 N, -102 E. Its UTM zone is 14N (EPSG:32614, central
 * meridian -99), where true north lies 2.5448022 degrees CLOCKWISE of grid
 * north (west of the central meridian); the project grid EPSG:26913 (NAD83
 * UTM 13N, central meridian -105) has true north 2.5448022 degrees
 * ANTICLOCKWISE of grid north.
 */
final class SurveyAzimuthReferenceTest extends TestCase
{
    private const THETA_LOCAL_32614 = 2.544802210;

    private const THETA_PROJECT_26913 = -2.544802210;

    /**
     * @return array<string, array{0: ?string, 1: ?string}>
     */
    public static function spellings(): array
    {
        return [
            'true' => ['true', 'true'],
            'true north, any case and spacing' => ['  True North ', 'true'],
            'true-north hyphenated' => ['true-north', 'true'],
            'tn' => ['TN', 'true'],
            'geographic' => ['Geographic', 'true'],
            'magnetic' => ['magnetic', 'magnetic'],
            'mag north' => ['Mag. North', 'magnetic'],
            'mn' => ['MN', 'magnetic'],
            'grid' => ['grid', 'grid'],
            'legacy grid_north stamp' => ['grid_north', 'grid'],
            'gn' => ['GN', 'grid'],
            'BOH declares nothing' => ['BOH', null],
            'TOH declares nothing' => ['toh', null],
            'unrecognised' => ['UTM', null],
            'a number' => ['12', null],
            'blank' => ['   ', null],
            'null' => [null, null],
        ];
    }

    #[DataProvider('spellings')]
    public function test_canonical(?string $raw, ?string $expected): void
    {
        $this->assertSame($expected, SurveyAzimuthReference::canonical($raw));
    }

    public function test_true_north_is_unchanged(): void
    {
        $r = SurveyAzimuthReference::toTrueNorth(90.0, 'true', null, null);
        $this->assertSame(['azimuth' => 90.0, 'unapplied' => false], $r);
    }

    public function test_magnetic_adds_the_east_positive_declination(): void
    {
        $this->assertEqualsWithDelta(102.0, SurveyAzimuthReference::toTrueNorth(90.0, 'magnetic', 12.0, null)['azimuth'], 1e-9);
        $this->assertEqualsWithDelta(78.0, SurveyAzimuthReference::toTrueNorth(90.0, 'magnetic', -12.0, null)['azimuth'], 1e-9);
    }

    public function test_magnetic_wraps_past_north(): void
    {
        $this->assertEqualsWithDelta(7.0, SurveyAzimuthReference::toTrueNorth(355.0, 'magnetic', 12.0, null)['azimuth'], 1e-9);
        $this->assertEqualsWithDelta(353.0, SurveyAzimuthReference::toTrueNorth(5.0, 'magnetic', -12.0, null)['azimuth'], 1e-9);
    }

    public function test_magnetic_with_no_declination_is_flagged_not_guessed(): void
    {
        $r = SurveyAzimuthReference::toTrueNorth(90.0, 'magnetic', null, null);
        $this->assertSame(['azimuth' => 90.0, 'unapplied' => true], $r);
    }

    public function test_grid_subtracts_the_bearing_of_true_north_in_that_grid(): void
    {
        // Local zone 32614: grid azimuth 90 is true azimuth 87.4551978.
        $local = SurveyAzimuthReference::toTrueNorth(90.0, 'grid', null, self::THETA_LOCAL_32614);
        $this->assertEqualsWithDelta(87.455197790, $local['azimuth'], 1e-8);
        $this->assertFalse($local['unapplied']);

        // Project grid 26913: grid azimuth 90 is true azimuth 92.5448022.
        $project = SurveyAzimuthReference::toTrueNorth(90.0, 'grid', null, self::THETA_PROJECT_26913);
        $this->assertEqualsWithDelta(92.544802210, $project['azimuth'], 1e-8);
    }

    public function test_grid_with_no_usable_grid_is_flagged_not_guessed(): void
    {
        $this->assertSame(
            ['azimuth' => 90.0, 'unapplied' => true],
            SurveyAzimuthReference::toTrueNorth(90.0, 'grid', null, null),
        );
    }

    public function test_an_undeclared_azimuth_is_used_as_recorded(): void
    {
        $this->assertSame(
            ['azimuth' => 123.4, 'unapplied' => false],
            SurveyAzimuthReference::toTrueNorth(123.4, null, 12.0, self::THETA_LOCAL_32614),
        );
    }

    public function test_the_result_stays_in_zero_to_360(): void
    {
        foreach ([0.0, 1.0, 179.9, 359.9] as $a) {
            foreach ([-12.0, 12.0] as $declination) {
                $z = SurveyAzimuthReference::toTrueNorth($a, 'magnetic', $declination, null)['azimuth'];
                $this->assertGreaterThanOrEqual(0.0, $z);
                $this->assertLessThan(360.0, $z);
            }
        }
    }
}
