<?php

declare(strict_types=1);

namespace Tests\Unit\Models;

use App\Models\Project;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\TestCase;

/**
 * Project::effectiveCrsEpsg() — what chat's context envelope is pre-filled with.
 *
 * Regression (Red Star, 2026-09-30): chat read the deprecated free-text
 * `crs_datum`, which every project is created with as `EPSG:32613`, so an
 * Alaska project created with EPSG:26904 was presented as UTM zone 13N.
 */
class ProjectEffectiveCrsEpsgTest extends TestCase
{
    public function test_a_new_project_carries_the_32613_datum_default(): void
    {
        // The trap this guards: the default is there whatever EPSG is chosen.
        $project = new Project(['project_name' => 'Red Star', 'crs_epsg' => 26904]);

        $this->assertSame('EPSG:32613', $project->crs_datum);
        $this->assertSame(26904, $project->effectiveCrsEpsg());
    }

    /**
     * @return array<string, array{0: int|string|null, 1: string|null, 2: int|null}>
     */
    public static function cases(): array
    {
        return [
            'crs_epsg wins over the datum default' => [26904, 'EPSG:32613', 26904],
            'crs_epsg as a numeric string' => ['26904', 'EPSG:32613', 26904],
            'no crs_epsg falls back to an EPSG datum' => [null, 'EPSG:26913', 26913],
            'lower-case datum' => [null, 'epsg:32606', 32606],
            'free-text datum is unspecified' => [null, 'NAD83 / UTM 4N', null],
            'out-of-range datum code is unspecified' => [null, 'EPSG:999999', null],
            'nothing set is unspecified' => [null, null, null],
            'zero crs_epsg is not a code' => [0, null, null],
        ];
    }

    #[DataProvider('cases')]
    public function test_effective_crs_epsg(int|string|null $crsEpsg, ?string $crsDatum, ?int $expected): void
    {
        $project = new Project;
        $project->crs_epsg = $crsEpsg;
        $project->crs_datum = $crsDatum;

        $this->assertSame($expected, $project->effectiveCrsEpsg());
    }
}
