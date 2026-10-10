<?php

declare(strict_types=1);

namespace Tests\Concerns;

use App\Models\Project;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use ZipArchive;

/**
 * Persisted collars (and their children) for the exporter tests, which read
 * silver.collars.geom_4326 through PostGIS and so run on Postgres only.
 *
 * Collars are inserted raw, the way the ingestion writes them. Their SOURCE
 * easting/northing are deliberately meaningless numbers: an exporter that
 * passes only by echoing the stored columns, the bug these tests pin, cannot.
 */
trait SeedsCollarExportData
{
    /**
     * @param int|null $crsEpsg the project's declared CRS, null for none
     */
    private function exportProject(?int $crsEpsg = null): Project
    {
        return Project::factory()->create($crsEpsg !== null ? ['crs_epsg' => $crsEpsg] : []);
    }

    /**
     * @param array{x: float, y: float, srid: int}|null $at the position as projected coordinates in `srid`
     *                                                      (converted to EPSG:4326 for geom_4326); null leaves geom_4326 NULL
     * @param array<string, mixed> $overrides
     */
    private function exportCollar(Project $project, string $holeId, ?array $at, array $overrides = []): string
    {
        $collarId = (string) Str::uuid();

        $row = array_merge([
            'collar_id' => $collarId,
            'hole_id' => $holeId,
            'project_id' => $project->project_id,
            'workspace_id' => $project->workspace_id,
            'easting' => 1650000.0,
            'northing' => 250000.0,
            'elevation' => 412.5,
            'total_depth' => 150.0,
            'hole_type' => 'Diamond',
            'azimuth' => 90.0,
            'dip' => -60.0,
            'drill_date' => '2024-06-15',
            'status' => 'Completed',
        ], $overrides);

        if ($at !== null) {
            $row['geom_4326'] = DB::raw(sprintf(
                'ST_Transform(ST_SetSRID(ST_MakePoint(%F, %F), %d), 4326)',
                $at['x'],
                $at['y'],
                $at['srid'],
            ));
        }

        DB::table('silver.collars')->insert($row);

        return (string) $row['collar_id'];
    }

    /**
     * @param array<string, mixed> $commodityAssays
     */
    private function exportSample(Project $project, string $collarId, float $from, float $to, array $commodityAssays): string
    {
        $sampleId = (string) Str::uuid();

        DB::table('silver.samples')->insert([
            'sample_id' => $sampleId,
            'collar_id' => $collarId,
            'workspace_id' => $project->workspace_id,
            'from_depth' => $from,
            'to_depth' => $to,
            'sample_type' => 'core',
            'commodity_assays' => json_encode($commodityAssays),
        ]);

        return $sampleId;
    }

    private function exportSurvey(Project $project, string $collarId, float $depth): void
    {
        DB::table('silver.surveys')->insert([
            'survey_id' => (string) Str::uuid(),
            'collar_id' => $collarId,
            'workspace_id' => $project->workspace_id,
            'depth' => $depth,
            'azimuth' => 90.0,
            'dip' => -60.0,
            'survey_method' => 'Gyro',
        ]);
    }

    /**
     * Rows of a CSV file, header first. The file is removed.
     *
     * @return list<list<string|null>>
     */
    private function readCsv(string $path): array
    {
        $rows = [];
        $handle = fopen($path, 'r');
        while (($row = fgetcsv($handle, 0, ',', '"', '')) !== false) {
            $rows[] = $row;
        }
        fclose($handle);
        @unlink($path);

        return $rows;
    }

    /**
     * Names of the entries in a ZIP, in archive order.
     *
     * @return list<string>
     */
    private function zipEntries(string $path): array
    {
        $zip = new ZipArchive;
        $this->assertTrue($zip->open($path) === true, 'the export is a readable ZIP');
        $names = [];
        for ($i = 0; $i < $zip->numFiles; $i++) {
            $names[] = (string) $zip->getNameIndex($i);
        }
        $zip->close();

        return $names;
    }

    private function zipEntry(string $path, string $name): string
    {
        $zip = new ZipArchive;
        $this->assertTrue($zip->open($path) === true, 'the export is a readable ZIP');
        $contents = $zip->getFromName($name);
        $zip->close();
        $this->assertNotFalse($contents, "the ZIP has an entry named {$name}");

        return (string) $contents;
    }

    /**
     * Parse CSV text, header first.
     *
     * @return list<list<string|null>>
     */
    private function parseCsv(string $text): array
    {
        $tmp = tempnam(sys_get_temp_dir(), 'csv_test_');
        file_put_contents($tmp, $text);

        return $this->readCsv($tmp);
    }
}
