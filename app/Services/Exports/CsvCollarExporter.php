<?php

declare(strict_types=1);

namespace App\Services\Exports;

use App\Models\Collar;
use Illuminate\Support\Collection;

/**
 * Exports collar records for a project as a plain CSV file.
 *
 * Column order matches the Micromine/Leapfrog collar table expectation:
 *   collar_id, hole_id, easting, northing, elevation, total_depth,
 *   hole_type, azimuth, dip, drill_date, status
 *
 * Returns an array with 'path' and 'size' so GenerateExportJob can upload
 * the file to MinIO and record the byte count.
 */
class CsvCollarExporter
{
    /**
     * @param  string     $projectId  UUID of the parent project.
     * @param  array      $filters    Optional row-level filters.
     * @return array{path: string, size: int}
     */
    public function export(string $projectId, array $filters = []): array
    {
        $collars = $this->fetchCollars($projectId, $filters);

        $tmpPath = sys_get_temp_dir() . '/georag_collars_' . uniqid() . '.csv';

        $handle = fopen($tmpPath, 'w');
        if ($handle === false) {
            throw new \RuntimeException("Cannot open temp file for writing: {$tmpPath}");
        }

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
            ]);

            foreach ($collars as $collar) {
                fputcsv($handle, [
                    $collar->collar_id,
                    $collar->hole_id,
                    $collar->easting,
                    $collar->northing,
                    $collar->elevation,
                    $collar->total_depth,
                    $collar->hole_type,
                    $collar->azimuth,
                    $collar->dip,
                    $collar->drill_date?->format('Y-m-d'),
                    $collar->status,
                ]);
            }
        } finally {
            fclose($handle);
        }

        return [
            'path' => $tmpPath,
            'size' => filesize($tmpPath),
        ];
    }

    private function fetchCollars(string $projectId, array $filters): Collection
    {
        $query = Collar::where('project_id', $projectId);

        if (!empty($filters['hole_type'])) {
            $query->where('hole_type', $filters['hole_type']);
        }

        if (!empty($filters['status'])) {
            $query->where('status', $filters['status']);
        }

        if (!empty($filters['drill_date_from'])) {
            $query->where('drill_date', '>=', $filters['drill_date_from']);
        }

        if (!empty($filters['drill_date_to'])) {
            $query->where('drill_date', '<=', $filters['drill_date_to']);
        }

        if (isset($filters['min_depth'])) {
            $query->where('total_depth', '>=', $filters['min_depth']);
        }

        if (isset($filters['max_depth'])) {
            $query->where('total_depth', '<=', $filters['max_depth']);
        }

        return $query->orderBy('hole_id')->get();
    }
}
