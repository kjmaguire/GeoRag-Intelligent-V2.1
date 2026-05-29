<?php

declare(strict_types=1);

namespace App\Services\Exports;

use Illuminate\Support\Facades\DB;

/**
 * Exports silver.assays rows for a project as a plain CSV file.
 *
 * Joins assays → samples → collars to scope by project_id, and emits
 * one row per (sample, element). QC flag and below-detection state are
 * preserved so downstream Python consumers can filter on them.
 *
 * For Shaun's prospectivity workflow this is the authoritative bulk
 * source — the per-hole analysis bundle endpoint returns aggregated
 * structures and is not designed for project-wide assay dumps.
 */
class CsvAssaysExporter
{
    /**
     * @param string $projectId UUID of the parent project.
     * @param array<string,mixed> $filters
     *                                     hole_id, element (eg "Au"), exclude_rejected (bool), include_below_detection (bool)
     *
     * @return array{path: string, size: int}
     */
    public function export(string $projectId, array $filters = []): array
    {
        $tmpPath = sys_get_temp_dir().'/georag_assays_'.uniqid().'.csv';
        $handle = fopen($tmpPath, 'w');
        if ($handle === false) {
            throw new \RuntimeException("Cannot open temp file for writing: {$tmpPath}");
        }

        try {
            fputcsv($handle, [
                'assay_id',
                'sample_id',
                'collar_id',
                'hole_id',
                'from_depth',
                'to_depth',
                'assay_element',
                'assay_value',
                'assay_unit',
                'method_code',
                'detection_limit',
                'below_detection',
                'qc_flag',
            ]);

            $query = DB::table('silver.assays as a')
                ->join('silver.samples as s', 'a.sample_id', '=', 's.sample_id')
                ->join('silver.collars as c', 's.collar_id', '=', 'c.collar_id')
                ->where('c.project_id', $projectId)
                ->select([
                    'a.assay_id',
                    'a.sample_id',
                    's.collar_id',
                    'c.hole_id',
                    's.from_depth',
                    's.to_depth',
                    'a.assay_element',
                    'a.assay_value',
                    'a.assay_unit',
                    'a.method_code',
                    'a.detection_limit',
                    'a.below_detection',
                    'a.qc_flag',
                ]);

            if (! empty($filters['hole_id'])) {
                $query->where('c.hole_id', $filters['hole_id']);
            }
            if (! empty($filters['element'])) {
                $query->where('a.assay_element', $filters['element']);
            }
            if (! empty($filters['exclude_rejected'])) {
                $query->where('a.qc_flag', '!=', 'rejected');
            }
            if (isset($filters['include_below_detection']) && $filters['include_below_detection'] === false) {
                $query->where('a.below_detection', false);
            }

            $query->orderBy('c.hole_id')
                ->orderBy('s.from_depth')
                ->orderBy('a.assay_element')
                ->chunk(2000, function ($rows) use ($handle) {
                    foreach ($rows as $row) {
                        fputcsv($handle, [
                            $row->assay_id,
                            $row->sample_id,
                            $row->collar_id,
                            $row->hole_id,
                            $row->from_depth,
                            $row->to_depth,
                            $row->assay_element,
                            $row->assay_value,
                            $row->assay_unit,
                            $row->method_code,
                            $row->detection_limit,
                            $row->below_detection ? 'true' : 'false',
                            $row->qc_flag,
                        ]);
                    }
                });
        } finally {
            fclose($handle);
        }

        return ['path' => $tmpPath, 'size' => filesize($tmpPath)];
    }
}
