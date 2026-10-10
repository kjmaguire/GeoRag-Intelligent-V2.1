<?php

declare(strict_types=1);

namespace Tests\Feature\Services\Exports;

use App\Services\Exports\CsaBundleExporter;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\Concerns\SeedsCollarExportData;
use Tests\TestCase;

/**
 * csa_bundle, run on persisted rows.
 *
 *  - assays.csv read `commodity_assays` keys `u3o8_ppm`, `au_ppb` and `cu_pct`.
 *    The ingestion writes canonical-case keys in normalised units (`U3O8_ppm`,
 *    `Au_ppm`, `Cu_pct`), so all three columns shipped empty for every project.
 *  - collars.csv wrote the source easting / northing with no CRS.
 *  - Surveys and samples were loaded whole, behind a `whereIn` of every collar
 *    id.
 *
 * Postgres-only: the coordinates go through PostGIS.
 */
final class CsaBundleExporterTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;
    use SeedsCollarExportData;

    private const AT = ['x' => 500000.0, 'y' => 6000000.0, 'srid' => 26913];

    /** @return list<array<string, string|null>> assays.csv rows keyed by header */
    private function assayRows(string $bundlePath): array
    {
        $rows = $this->parseCsv($this->zipEntry($bundlePath, 'assays.csv'));
        $header = $rows[0];
        $this->assertSame(
            ['hole_id', 'from_depth', 'to_depth', 'sample_type', 'u3o8_ppm', 'au_ppb', 'cu_pct'],
            $header,
        );

        return array_map(fn (array $r): array => array_combine($header, $r), array_slice($rows, 1));
    }

    public function test_the_bundle_has_the_three_files(): void
    {
        $project = $this->exportProject(26913);
        $this->exportCollar($project, 'CSA-1', self::AT);

        $path = (new CsaBundleExporter)->export($project->project_id)['path'];

        $this->assertSame(['collars.csv', 'surveys.csv', 'assays.csv'], $this->zipEntries($path));
        @unlink($path);
    }

    public function test_gold_stored_in_ppm_fills_the_ppb_column(): void
    {
        // The case from the audit: {"Au_ppm": 1.2} must read 1200 ppb, not blank.
        $project = $this->exportProject(26913);
        $collar = $this->exportCollar($project, 'CSA-2', self::AT);
        $this->exportSample($project, $collar, 0.0, 1.0, ['Au_ppm' => 1.2]);

        $path = (new CsaBundleExporter)->export($project->project_id)['path'];
        $rows = $this->assayRows($path);
        @unlink($path);

        $this->assertCount(1, $rows);
        $this->assertSame('CSA-2', $rows[0]['hole_id']);
        $this->assertEquals(1200, $rows[0]['au_ppb']);
        $this->assertSame('', (string) $rows[0]['u3o8_ppm']);
        $this->assertSame('', (string) $rows[0]['cu_pct']);
    }

    public function test_canonical_keys_in_the_columns_own_units_are_passed_through(): void
    {
        $project = $this->exportProject(26913);
        $collar = $this->exportCollar($project, 'CSA-3', self::AT);
        $this->exportSample($project, $collar, 0.0, 1.0, ['U3O8_ppm' => 1250, 'Au_ppb' => 45, 'Cu_pct' => 0.35]);

        $path = (new CsaBundleExporter)->export($project->project_id)['path'];
        $rows = $this->assayRows($path);
        @unlink($path);

        $this->assertEquals(1250, $rows[0]['u3o8_ppm']);
        $this->assertEquals(45, $rows[0]['au_ppb']);
        $this->assertEquals(0.35, $rows[0]['cu_pct']);
    }

    public function test_other_units_are_converted_and_equivalents_are_not_mistaken_for_assays(): void
    {
        $project = $this->exportProject(26913);
        $collar = $this->exportCollar($project, 'CSA-4', self::AT);
        $this->exportSample($project, $collar, 0.0, 1.0, [
            'Cu_ppm' => 3500,          // -> 0.35 %
            'U3O8_pct' => 0.125,       // -> 1250 ppm
            'U3O8_pct_e' => 9.9,       // a radiometric equivalent: not a chemical assay
            'au_ppm' => 0.07,          // legacy lower-case key, still found
            'n_points' => 4,           // bookkeeping
        ]);

        $path = (new CsaBundleExporter)->export($project->project_id)['path'];
        $rows = $this->assayRows($path);
        @unlink($path);

        $this->assertEquals(0.35, $rows[0]['cu_pct']);
        $this->assertEquals(1250, $rows[0]['u3o8_ppm']);
        $this->assertSame('70', $rows[0]['au_ppb'], '0.07 ppm is exactly 70 ppb, with no float noise');
    }

    public function test_collars_csv_carries_projected_coordinates_and_their_epsg(): void
    {
        $project = $this->exportProject(26913);
        $this->exportCollar($project, 'CSA-5', self::AT);

        $path = (new CsaBundleExporter)->export($project->project_id)['path'];
        $rows = $this->parseCsv($this->zipEntry($path, 'collars.csv'));
        @unlink($path);

        $this->assertSame(['hole_id', 'easting', 'northing', 'elevation', 'total_depth', 'azimuth', 'dip', 'epsg'], $rows[0]);
        $this->assertSame('CSA-5', $rows[1][0]);
        $this->assertEqualsWithDelta(500000.0, (float) $rows[1][1], 0.01);
        $this->assertEqualsWithDelta(6000000.0, (float) $rows[1][2], 0.01);
        $this->assertSame('26913', $rows[1][7]);
    }

    public function test_filters_apply_to_the_children_as_well_as_the_collars(): void
    {
        $project = $this->exportProject(26913);
        $keep = $this->exportCollar($project, 'KEEP', self::AT, ['hole_type' => 'Diamond']);
        $drop = $this->exportCollar($project, 'DROP', self::AT, ['hole_type' => 'RC']);
        foreach ([$keep, $drop] as $collar) {
            $this->exportSample($project, $collar, 0.0, 1.0, ['Au_ppm' => 1.0]);
            $this->exportSurvey($project, $collar, 10.0);
        }

        $path = (new CsaBundleExporter)->export($project->project_id, ['hole_type' => 'diamond'])['path'];
        $collars = $this->parseCsv($this->zipEntry($path, 'collars.csv'));
        $surveys = $this->parseCsv($this->zipEntry($path, 'surveys.csv'));
        $assays = $this->assayRows($path);
        @unlink($path);

        $this->assertSame(['KEEP'], array_column(array_slice($collars, 1), 0));
        $this->assertSame(['KEEP'], array_column(array_slice($surveys, 1), 0));
        $this->assertSame(['KEEP'], array_column($assays, 'hole_id'));
    }

    public function test_child_tables_are_read_in_pages_without_dropping_or_repeating_rows(): void
    {
        // More surveys than one page (2,000), including rows tied on depth, so
        // the paging order has to be total to come out whole.
        $project = $this->exportProject(26913);
        $collar = $this->exportCollar($project, 'BIG', self::AT);

        $rows = [];
        for ($i = 0; $i < 2300; $i++) {
            $rows[] = [
                'survey_id' => (string) Str::uuid(),
                'collar_id' => $collar,
                'workspace_id' => $project->workspace_id,
                'depth' => (float) intdiv($i, 3),   // three stations per depth
                'azimuth' => 90.0,
                'dip' => -60.0,
                'survey_method' => 'Gyro',
            ];
        }
        foreach (array_chunk($rows, 500) as $chunk) {
            DB::table('silver.surveys')->insert($chunk);
        }

        $path = (new CsaBundleExporter)->export($project->project_id)['path'];
        $surveys = array_slice($this->parseCsv($this->zipEntry($path, 'surveys.csv')), 1);
        @unlink($path);

        $this->assertCount(2300, $surveys);
        $depths = array_map(fn (array $r): float => (float) $r[1], $surveys);
        $sorted = $depths;
        sort($sorted);
        $this->assertSame($sorted, $depths, 'ordered by depth');
    }

    public function test_a_failed_export_leaves_no_bundle_behind(): void
    {
        $project = $this->exportProject(26913);
        $this->exportCollar($project, 'CSA-6', self::AT);
        // A filter value PostgreSQL rejects as a date: the collar query fails
        // after the ZIP's path has been chosen.
        $before = glob(sys_get_temp_dir().'/georag_csa_*') ?: [];

        try {
            (new CsaBundleExporter)->export($project->project_id, ['drill_date_from' => 'not-a-date']);
            $this->fail('the export should have failed');
        } catch (\Throwable) {
            // expected
        }

        $this->assertSame([], array_values(array_diff(glob(sys_get_temp_dir().'/georag_csa_*') ?: [], $before)));
    }
}
