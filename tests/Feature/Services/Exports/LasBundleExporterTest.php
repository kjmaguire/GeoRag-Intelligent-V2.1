<?php

declare(strict_types=1);

namespace Tests\Feature\Services\Exports;

use App\Models\Project;
use App\Services\Exports\LasBundleExporter;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\Concerns\SeedsCollarExportData;
use Tests\TestCase;

/**
 * las_bundle, run on persisted rows.
 *
 *  - A real hole id ("LEB 23/001") was put straight into a temp path and a ZIP
 *    entry name, where "/" is a directory separator.
 *  - The ~WELL section located the well by the stored easting / northing, with
 *    no CRS.
 *  - Every collar's id went into one whereIn and every curve of every collar
 *    was loaded before the first file was written.
 *
 * Postgres-only: the coordinates go through PostGIS and the curve depths are
 * float8[] columns.
 */
final class LasBundleExporterTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;
    use SeedsCollarExportData;

    private const AT = ['x' => 500000.0, 'y' => 6000000.0, 'srid' => 26913];

    private function curve(Project $project, string $collarId, string $name, string $depths = '{0,1,2}', string $values = '{10,11,12}', ?string $depthUnit = null): void
    {
        DB::table('silver.well_log_curves')->insert([
            'curve_id' => (string) Str::uuid(),
            'collar_id' => $collarId,
            'workspace_id' => $project->workspace_id,
            'curve_name' => $name,
            'curve_unit' => 'GAPI',
            'min_depth' => 0,
            'max_depth' => 2,
            'step' => 1,
            'sample_count' => count(explode(',', trim($depths, '{}'))),
            'las_version' => '2.0',
            'depths' => $depths,
            'values' => $values,
            'depth_unit' => $depthUnit,
        ]);
    }

    public function test_curves_logged_on_different_depths_are_not_zipped_into_one_table(): void
    {
        // Two runs: GR at 1 m and DEN at 0.5 m. Written side by side, DEN's
        // third value (at 1.0 m) used to land on GR's third depth (2.0 m).
        $project = $this->exportProject(26913);
        $collar = $this->exportCollar($project, 'RUNS-1', self::AT);
        $this->curve($project, $collar, 'GR', '{0,1,2}', '{10,11,12}', 'm');
        $this->curve($project, $collar, 'DEN', '{0,0.5,1,1.5,2}', '{2.1,2.2,2.3,2.4,2.5}', 'm');

        $path = (new LasBundleExporter)->export($project->project_id)['path'];
        $entries = $this->zipEntries($path);
        sort($entries);
        $files = array_map(fn (string $entry): string => $this->zipEntry($path, $entry), $entries);
        @unlink($path);

        $this->assertSame(['RUNS-1-2.las', 'RUNS-1.las'], $entries);
        $den = str_contains($files[0], 'DEN.') ? $files[0] : $files[1];
        $gr = $den === $files[0] ? $files[1] : $files[0];
        $this->assertStringNotContainsString('GR.', $den);
        $this->assertStringNotContainsString('DEN.', $gr);
        $this->assertStringContainsString("1.0000    2.3000\n", $den);
        $this->assertStringContainsString("2.0000    12.0000\n", $gr);
    }

    public function test_the_depth_unit_header_follows_the_stored_unit(): void
    {
        $project = $this->exportProject(26913);
        $metric = $this->exportCollar($project, 'UNIT-M', self::AT);
        $legacy = $this->exportCollar($project, 'UNIT-X', self::AT);
        $this->curve($project, $metric, 'GR', depthUnit: 'm');
        $this->curve($project, $legacy, 'GR');

        $path = (new LasBundleExporter)->export($project->project_id)['path'];
        $metres = $this->zipEntry($path, 'UNIT-M.las');
        $unknown = $this->zipEntry($path, 'UNIT-X.las');
        @unlink($path);

        $this->assertStringContainsString('STRT.M ', $metres);
        $this->assertStringContainsString('DEPT.M ', $metres);
        // A legacy row's depths are in its source file's unrecorded unit:
        // the header must not claim metres for them.
        $this->assertStringNotContainsString('STRT.M', $unknown);
        $this->assertStringNotContainsString('DEPT.M', $unknown);
        $this->assertStringContainsString('unit not recorded', $unknown);
    }

    public function test_a_collar_with_no_elevation_writes_no_elevation(): void
    {
        $project = $this->exportProject(26913);
        $collar = $this->exportCollar($project, 'NO-ELEV', self::AT, ['elevation' => null]);
        $this->curve($project, $collar, 'GR', depthUnit: 'm');

        $path = (new LasBundleExporter)->export($project->project_id)['path'];
        $las = $this->zipEntry($path, 'NO-ELEV.las');
        @unlink($path);

        $this->assertStringNotContainsString('ELEV.', $las);
    }

    public function test_a_hole_id_with_a_slash_and_a_space_becomes_a_safe_entry_name(): void
    {
        $project = $this->exportProject(26913);
        $collar = $this->exportCollar($project, 'LEB 23/001', self::AT);
        $this->curve($project, $collar, 'GR');

        $path = (new LasBundleExporter)->export($project->project_id)['path'];
        $entries = $this->zipEntries($path);
        $las = $this->zipEntry($path, $entries[0]);
        @unlink($path);

        $this->assertSame(['LEB_23_001.las'], $entries);
        // The header keeps the REAL hole id; only the file name is made safe.
        $this->assertStringContainsString('WELL.                  LEB 23/001 : Well name', $las);
    }

    public function test_ids_that_collapse_to_one_name_do_not_overwrite_each_other(): void
    {
        // Three different holes (their canonical ids differ, so the database
        // accepts them) whose names all sanitise to "A_1" -- two of them
        // differing only in case, which Windows and macOS fold on extraction.
        $project = $this->exportProject(26913);
        foreach (['A:1', 'A#1', 'a*1'] as $hole) {
            $this->curve($project, $this->exportCollar($project, $hole, self::AT), 'GR');
        }

        $path = (new LasBundleExporter)->export($project->project_id)['path'];
        $entries = $this->zipEntries($path);
        @unlink($path);

        $this->assertCount(3, $entries);
        $this->assertCount(3, array_unique(array_map('strtolower', $entries)), 'unique even ignoring case');
        foreach ($entries as $entry) {
            $this->assertMatchesRegularExpression('/^[A-Za-z0-9._-]+\.las$/', $entry);
        }
    }

    public function test_a_traversing_hole_id_cannot_escape_the_extraction_folder(): void
    {
        $project = $this->exportProject(26913);
        $this->curve($project, $this->exportCollar($project, '../../etc/passwd', self::AT), 'GR');
        $this->curve($project, $this->exportCollar($project, '..', self::AT), 'GR');

        $path = (new LasBundleExporter)->export($project->project_id)['path'];
        $entries = $this->zipEntries($path);
        @unlink($path);

        foreach ($entries as $entry) {
            $this->assertStringNotContainsString('/', $entry);
            $this->assertStringNotContainsString('..', $entry);
        }
        $this->assertCount(2, $entries);
    }

    public function test_the_well_section_locates_the_collar_in_a_stated_crs(): void
    {
        $project = $this->exportProject(26913);
        $collar = $this->exportCollar($project, 'LAS-1', self::AT, ['elevation' => 412.5]);
        $this->curve($project, $collar, 'GR');

        $path = (new LasBundleExporter)->export($project->project_id)['path'];
        $las = $this->zipEntry($path, 'LAS-1.las');
        @unlink($path);

        // Not the stored 1650000 / 250000.
        $this->assertStringContainsString('LOC .                  E500000.00 N6000000.00 : Location (Easting Northing)', $las);
        $this->assertStringContainsString('HZCS.                  EPSG:26913 : Horizontal coordinate system', $las);
        $this->assertStringNotContainsString('1650000', $las);
        $this->assertStringContainsString('ELEV.M                 412.50 : Elevation', $las);
    }

    public function test_a_collar_with_no_geometry_says_its_location_is_unknown(): void
    {
        $project = $this->exportProject(26913);
        $this->curve($project, $this->exportCollar($project, 'LAS-2', null), 'GR');

        $path = (new LasBundleExporter)->export($project->project_id)['path'];
        $las = $this->zipEntry($path, 'LAS-2.las');
        @unlink($path);

        $this->assertStringContainsString('LOC .                  UNKNOWN', $las);
        $this->assertStringNotContainsString('HZCS.', $las);
    }

    public function test_curves_are_written_with_their_depths_and_values(): void
    {
        $project = $this->exportProject(26913);
        $collar = $this->exportCollar($project, 'LAS-3', self::AT);
        $this->curve($project, $collar, 'GR', '{0,1,2}', '{10,11,12}');
        $this->curve($project, $collar, 'RHOB', '{0,1,2}', '{2.5,2.6,2.7}');

        $path = (new LasBundleExporter)->export($project->project_id)['path'];
        $las = $this->zipEntry($path, 'LAS-3.las');
        @unlink($path);

        $this->assertStringContainsString("0.0000    10.0000    2.5000\n", $las);
        $this->assertStringContainsString("2.0000    12.0000    2.7000\n", $las);
    }

    public function test_only_collars_with_curves_are_exported_and_none_gives_a_notice(): void
    {
        $project = $this->exportProject(26913);
        $withCurves = $this->exportCollar($project, 'HAS-LOG', self::AT);
        $this->exportCollar($project, 'NO-LOG', self::AT);
        $this->curve($project, $withCurves, 'GR');

        $path = (new LasBundleExporter)->export($project->project_id)['path'];
        $entries = $this->zipEntries($path);
        @unlink($path);
        $this->assertSame(['HAS-LOG.las'], $entries);

        $empty = $this->exportProject(26913);
        $this->exportCollar($empty, 'NO-LOG', self::AT);
        $path = (new LasBundleExporter)->export($empty->project_id)['path'];
        $entries = $this->zipEntries($path);
        $this->assertStringContainsString('No well-log curves were found', $this->zipEntry($path, 'README.txt'));
        @unlink($path);
        $this->assertSame(['README.txt'], $entries);
    }

    public function test_no_temporary_files_are_left_behind(): void
    {
        $project = $this->exportProject(26913);
        $this->curve($project, $this->exportCollar($project, 'TIDY', self::AT), 'GR');
        $before = glob(sys_get_temp_dir().'/georag_las_*') ?: [];

        $path = (new LasBundleExporter)->export($project->project_id)['path'];
        @unlink($path);

        $this->assertSame([], array_values(array_diff(glob(sys_get_temp_dir().'/georag_las_*') ?: [], $before)));
    }
}
