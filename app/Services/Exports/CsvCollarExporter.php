<?php

declare(strict_types=1);

namespace App\Services\Exports;

use Illuminate\Support\Facades\Log;

/**
 * Exports collar records for a project as a plain CSV file.
 *
 * Column order matches the Micromine/Leapfrog collar table expectation, with
 * the CRS of the coordinates appended so it can be mapped by an importer that
 * reads by position:
 *   collar_id, hole_id, easting, northing, elevation, total_depth,
 *   hole_type, azimuth, dip, drill_date, status, epsg
 *
 * easting / northing are NOT the values the collar file was uploaded with --
 * those carry no CRS in silver.collars. They are the collar's position in the
 * CRS named by `epsg` (the project's projected CRS, else the collar's UTM
 * zone), the same rule the shapefile and GeoPackage exports use; see
 * CollarExportQuery. `epsg` states the unit too: metres for UTM, feet for a US
 * state plane. A collar with no recorded position has all three blank.
 *
 * hole_type and status are written as stored: the ingestion keeps the file's
 * own words, which are not always in the §04e vocabulary.
 *
 * Returns an array with 'path' and 'size' so GenerateExportJob can upload
 * the file to MinIO and record the byte count.
 */
class CsvCollarExporter
{
    /**
     * @param string $projectId UUID of the parent project.
     * @param array $filters Optional row-level filters.
     *
     * @return array{path: string, size: int}
     */
    public function export(string $projectId, array $filters = []): array
    {
        $tmpPath = sys_get_temp_dir().'/georag_collars_'.uniqid().'.csv';

        $handle = fopen($tmpPath, 'w');
        if ($handle === false) {
            throw new \RuntimeException("Cannot open temp file for writing: {$tmpPath}");
        }

        $unplaced = 0;

        try {
            // Write header row.
            fputcsv($handle, [
                'collar_id',
                'hole_id',
                'easting',
                'northing',
                'elevation',
                'total_depth',
                'hole_type',
                'azimuth',
                'dip',
                'drill_date',
                'status',
                'epsg',
            ], escape: '');

            $collars = CollarExportQuery::forProject($projectId, $filters)
                ->orderBy('hole_id')
                ->orderBy('collar_id')
                ->cursor();

            foreach ($collars as $collar) {
                if ($collar->export_epsg === null) {
                    $unplaced++;
                }

                fputcsv($handle, [
                    $collar->collar_id,
                    $collar->hole_id,
                    CollarExportQuery::coordinate($collar->export_easting),
                    CollarExportQuery::coordinate($collar->export_northing),
                    $collar->elevation,
                    $collar->total_depth,
                    $collar->getRawOriginal('hole_type'),
                    $collar->azimuth,
                    $collar->dip,
                    $collar->drill_date?->format('Y-m-d'),
                    $collar->getRawOriginal('status'),
                    $collar->export_epsg,
                ], escape: '');
            }
        } finally {
            fclose($handle);
        }

        if ($unplaced > 0) {
            Log::warning('csv_collars export: collars with no geom_4326 were written without coordinates', [
                'project_id' => $projectId,
                'collars' => $unplaced,
            ]);
        }

        return [
            'path' => $tmpPath,
            'size' => filesize($tmpPath),
        ];
    }
}
