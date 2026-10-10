<?php

declare(strict_types=1);

namespace Tests\Feature\Services\Exports;

use App\Services\Exports\CsvCollarExporter;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\Concerns\RequiresPostgres;
use Tests\Concerns\SeedsCollarExportData;
use Tests\TestCase;

/**
 * csv_collars, run on persisted rows.
 *
 * Two defects shared this exporter:
 *
 *  - It handed the HoleType / CollarStatus ENUMS (the model casts) straight to
 *    fputcsv. A backed enum has no __toString, so a project with any collar at
 *    all raised an Error and the export failed.
 *  - It wrote silver.collars.easting / northing, the SOURCE values, which carry
 *    no CRS (UTM of any zone, US-foot state plane, sometimes degrees). The
 *    coordinates now come from geom_4326 in a CRS the file names.
 *
 * Postgres-only: the coordinates go through PostGIS.
 */
final class CsvCollarExporterTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;
    use SeedsCollarExportData;

    private const HEADER = [
        'collar_id', 'hole_id', 'easting', 'northing', 'elevation', 'total_depth',
        'hole_type', 'azimuth', 'dip', 'drill_date', 'status', 'epsg',
    ];

    /** @return array<string, string|null> the single data row, keyed by header */
    private function onlyRow(string $path): array
    {
        $rows = $this->readCsv($path);
        $this->assertSame(self::HEADER, $rows[0]);
        $this->assertCount(2, $rows, 'a header and exactly one collar');

        return array_combine(self::HEADER, $rows[1]);
    }

    public function test_a_persisted_collar_exports_without_an_error(): void
    {
        // THE BUG: `fputcsv($handle, [... $collar->hole_type ...])` with the
        // enum the model casts to. Any project with a collar threw here.
        $project = $this->exportProject(26913);
        $this->exportCollar($project, 'EXP-001', ['x' => 500000.0, 'y' => 6000000.0, 'srid' => 26913]);

        $row = $this->onlyRow((new CsvCollarExporter)->export($project->project_id)['path']);

        $this->assertSame('EXP-001', $row['hole_id']);
        $this->assertSame('Diamond', $row['hole_type']);
        $this->assertSame('Completed', $row['status']);
        $this->assertSame('2024-06-15', $row['drill_date']);
        $this->assertEquals(412.5, $row['elevation']);
        $this->assertEquals(150.0, $row['total_depth']);
    }

    public function test_a_value_with_a_backslash_before_a_quote_round_trips_as_rfc_4180(): void
    {
        // fputcsv's default escape ("\") wrote `"Q\"1"`, which a standard
        // CSV reader (Excel, pandas, an import into a modelling package) takes
        // apart differently; PHP 8.4 also deprecates relying on that default.
        $project = $this->exportProject(26913);
        $this->exportCollar($project, 'Q\\"1', ['x' => 500000.0, 'y' => 6000000.0, 'srid' => 26913]);

        $row = $this->onlyRow((new CsvCollarExporter)->export($project->project_id)['path']);

        $this->assertSame('Q\\"1', $row['hole_id']);
    }

    public function test_the_stored_words_of_a_file_are_exported_as_they_are(): void
    {
        // The ingestion keeps what the file said ("DDH", "Closed"). The model
        // reads those as null; the export must still carry them.
        $project = $this->exportProject(26913);
        $this->exportCollar(
            $project,
            'EXP-002',
            ['x' => 500000.0, 'y' => 6000000.0, 'srid' => 26913],
            ['hole_type' => 'DDH', 'status' => 'Closed'],
        );

        $row = $this->onlyRow((new CsvCollarExporter)->export($project->project_id)['path']);

        $this->assertSame('DDH', $row['hole_type']);
        $this->assertSame('Closed', $row['status']);
    }

    public function test_coordinates_come_from_the_geometry_in_the_projects_crs_not_the_source_columns(): void
    {
        // The stored easting/northing (1650000 / 250000) are the upload's own
        // frame; the true position is UTM 13N 500000 / 6000000.
        $project = $this->exportProject(26913);
        $this->exportCollar($project, 'EXP-003', ['x' => 500000.0, 'y' => 6000000.0, 'srid' => 26913]);

        $row = $this->onlyRow((new CsvCollarExporter)->export($project->project_id)['path']);

        $this->assertEqualsWithDelta(500000.0, (float) $row['easting'], 0.01);
        $this->assertEqualsWithDelta(6000000.0, (float) $row['northing'], 0.01);
        $this->assertSame('26913', $row['epsg']);
    }

    public function test_a_us_foot_state_plane_project_is_exported_in_feet_with_its_epsg(): void
    {
        // EPSG:2263 NAD83 / New York Long Island (ftUS): the epsg column is how
        // a reader learns the numbers are feet, so it must be the one used.
        $project = $this->exportProject(2263);
        $this->exportCollar($project, 'EXP-004', ['x' => 1000000.0, 'y' => 200000.0, 'srid' => 2263]);

        $row = $this->onlyRow((new CsvCollarExporter)->export($project->project_id)['path']);

        $this->assertSame('2263', $row['epsg']);
        $this->assertEqualsWithDelta(1000000.0, (float) $row['easting'], 0.01);
        $this->assertEqualsWithDelta(200000.0, (float) $row['northing'], 0.01);
    }

    public function test_without_a_projected_project_crs_each_collar_gets_its_own_utm_zone(): void
    {
        // No crs_epsg: the rule FastAPI's shapefile/GeoPackage export uses --
        // the UTM zone the collar sits in (326xx north, 327xx south).
        $project = $this->exportProject();
        // Eastings near each zone's central meridian (500000), so rounding the
        // longitude into a zone cannot land them in a neighbour.
        $this->exportCollar($project, 'N-13', ['x' => 500000.0, 'y' => 6000000.0, 'srid' => 32613]);
        $this->exportCollar($project, 'N-12', ['x' => 450000.0, 'y' => 6100000.0, 'srid' => 32612]);
        $this->exportCollar($project, 'S-55', ['x' => 500000.0, 'y' => 4000000.0, 'srid' => 32755]);

        $rows = $this->readCsv((new CsvCollarExporter)->export($project->project_id)['path']);
        $byHole = [];
        foreach (array_slice($rows, 1) as $r) {
            $byHole[$r[1]] = array_combine(self::HEADER, $r);
        }

        $this->assertSame('32613', $byHole['N-13']['epsg']);
        $this->assertSame('32612', $byHole['N-12']['epsg']);
        $this->assertSame('32755', $byHole['S-55']['epsg']);
        $this->assertEqualsWithDelta(500000.0, (float) $byHole['N-13']['easting'], 0.01);
        $this->assertEqualsWithDelta(6100000.0, (float) $byHole['N-12']['northing'], 0.01);
        $this->assertEqualsWithDelta(4000000.0, (float) $byHole['S-55']['northing'], 0.01);
    }

    public function test_a_geographic_project_crs_is_not_used_for_easting_and_northing(): void
    {
        // EPSG:4326 is not PROJCS: degrees are not an easting. Falls back to UTM.
        $project = $this->exportProject(4326);
        $this->exportCollar($project, 'EXP-005', ['x' => 500000.0, 'y' => 6000000.0, 'srid' => 32613]);

        $row = $this->onlyRow((new CsvCollarExporter)->export($project->project_id)['path']);

        $this->assertSame('32613', $row['epsg']);
        $this->assertEqualsWithDelta(500000.0, (float) $row['easting'], 0.01);
    }

    public function test_a_collar_with_no_geometry_is_not_given_a_position_it_cannot_justify(): void
    {
        $project = $this->exportProject(26913);
        $this->exportCollar($project, 'EXP-006', null);

        $row = $this->onlyRow((new CsvCollarExporter)->export($project->project_id)['path']);

        // Blank, not the stored 1650000 / 250000 with nothing to say what they are.
        $this->assertSame('', (string) $row['easting']);
        $this->assertSame('', (string) $row['northing']);
        $this->assertSame('', (string) $row['epsg']);
        $this->assertSame('EXP-006', $row['hole_id']);
    }

    public function test_hole_type_and_status_filters_ignore_case(): void
    {
        // The request validates "Active"; the ingestion writes 'active'. A
        // case-sensitive match returned nothing for a satisfied filter.
        $project = $this->exportProject(26913);
        $at = ['x' => 500000.0, 'y' => 6000000.0, 'srid' => 26913];
        $this->exportCollar($project, 'KEEP', $at, ['hole_type' => 'Diamond', 'status' => 'active']);
        $this->exportCollar($project, 'DROP-TYPE', $at, ['hole_type' => 'RC', 'status' => 'active']);
        $this->exportCollar($project, 'DROP-STATUS', $at, ['hole_type' => 'Diamond', 'status' => 'Abandoned']);

        $rows = $this->readCsv((new CsvCollarExporter)->export($project->project_id, [
            'hole_type' => 'diamond',
            'status' => 'ACTIVE',
        ])['path']);

        $this->assertSame(['KEEP'], array_column(array_slice($rows, 1), 1));
    }

    public function test_the_other_row_filters_still_apply(): void
    {
        $project = $this->exportProject(26913);
        $at = ['x' => 500000.0, 'y' => 6000000.0, 'srid' => 26913];
        $this->exportCollar($project, 'SHALLOW', $at, ['total_depth' => 50.0, 'drill_date' => '2020-01-01']);
        $this->exportCollar($project, 'DEEP', $at, ['total_depth' => 400.0, 'drill_date' => '2024-01-01']);

        $deep = $this->readCsv((new CsvCollarExporter)->export($project->project_id, ['min_depth' => 100])['path']);
        $recent = $this->readCsv((new CsvCollarExporter)->export($project->project_id, ['drill_date_from' => '2023-01-01'])['path']);

        $this->assertSame(['DEEP'], array_column(array_slice($deep, 1), 1));
        $this->assertSame(['DEEP'], array_column(array_slice($recent, 1), 1));
    }

    public function test_another_projects_collars_are_not_exported(): void
    {
        $mine = $this->exportProject(26913);
        $other = $this->exportProject(26913);
        $at = ['x' => 500000.0, 'y' => 6000000.0, 'srid' => 26913];
        $this->exportCollar($mine, 'MINE', $at);
        $this->exportCollar($other, 'THEIRS', $at);

        $rows = $this->readCsv((new CsvCollarExporter)->export($mine->project_id)['path']);

        $this->assertSame(['MINE'], array_column(array_slice($rows, 1), 1));
    }
}
