<?php

declare(strict_types=1);

namespace App\Services\Exports;

use Illuminate\Support\Facades\DB;

/**
 * Exports silver.samples rows for a project as a plain CSV file.
 *
 * Joins through silver.collars to scope by project_id. Provenance:
 * collar_id is the link back to the parent hole, sample_id is the
 * stable per-sample identifier — a Python consumer can re-join either
 * via the existing /api/v1/projects/{p}/collars and /api/v1/projects/
 * {p}/holes/{h}/analysis endpoints.
 *
 * Column order is geologist-conventional (collar → depth → sample id).
 */
class CsvSamplesExporter
{
    /**
     * @param string $projectId UUID of the parent project.
     * @param array<string,mixed> $filters Optional row-level filters
     *                                     supported: hole_id, from_depth_min, from_depth_max, sample_type.
     *
     * @return array{path: string, size: int}
     */
    public function export(string $projectId, array $filters = []): array
    {
        $tmpPath = sys_get_temp_dir().'/georag_samples_'.uniqid().'.csv';
        $handle = fopen($tmpPath, 'w');
        if ($handle === false) {
            throw new \RuntimeException("Cannot open temp file for writing: {$tmpPath}");
        }

        try {
            fputcsv($handle, [
                'sample_id',
                'collar_id',
                'hole_id',
                'from_depth',
                'to_depth',
                'sample_type',
                'lab_id',
                'qaqc_type',
            ]);

            $query = DB::table('silver.samples as s')
                ->join('silver.collars as c', 's.collar_id', '=', 'c.collar_id')
                ->where('c.project_id', $projectId)
                ->select([
                    's.sample_id',
                    's.collar_id',
                    'c.hole_id',
                    's.from_depth',
                    's.to_depth',
                    's.sample_type',
                    's.lab_id',
                    's.qaqc_type',
                ]);

            if (! empty($filters['hole_id'])) {
                $query->where('c.hole_id', $filters['hole_id']);
            }
            if (isset($filters['from_depth_min'])) {
                $query->where('s.from_depth', '>=', $filters['from_depth_min']);
            }
            if (isset($filters['from_depth_max'])) {
                $query->where('s.from_depth', '<=', $filters['from_depth_max']);
            }
            if (! empty($filters['sample_type'])) {
                $query->where('s.sample_type', $filters['sample_type']);
            }

            $query->orderBy('c.hole_id')
                ->orderBy('s.from_depth')
                ->chunk(2000, function ($rows) use ($handle) {
                    foreach ($rows as $row) {
                        fputcsv($handle, [
                            $row->sample_id,
                            $row->collar_id,
                            $row->hole_id,
                            $row->from_depth,
                            $row->to_depth,
                            $row->sample_type,
                            $row->lab_id,
                            $row->qaqc_type,
                        ]);
                    }
                });
        } finally {
            fclose($handle);
        }

        return ['path' => $tmpPath, 'size' => filesize($tmpPath)];
    }
}
