<?php

declare(strict_types=1);

namespace App\Services\Exports;

use Illuminate\Support\Facades\DB;

/**
 * Exports silver.lithology rows for a project as a plain CSV file.
 *
 * Uses the new silver.lithology table (rock_code + rock_code_confidence)
 * NOT the legacy silver.lithology_logs. Includes the fuzzy-match
 * confidence column added in CC-02 Item 1 so consumers can filter for
 * "exact catalogue hits only" (confidence = 1.0) or "needs review"
 * (confidence < 1.0). NULL confidence means catalogue gap.
 */
class CsvLithologyExporter
{
    /**
     * @param string $projectId UUID of the parent project.
     * @param array<string,mixed> $filters
     *                                     hole_id, min_confidence (float, default 0.0)
     *
     * @return array{path: string, size: int}
     */
    public function export(string $projectId, array $filters = []): array
    {
        $tmpPath = sys_get_temp_dir().'/georag_lithology_'.uniqid().'.csv';
        $handle = fopen($tmpPath, 'w');
        if ($handle === false) {
            throw new \RuntimeException("Cannot open temp file for writing: {$tmpPath}");
        }

        try {
            fputcsv($handle, [
                'id',
                'collar_id',
                'hole_id',
                'from_depth',
                'to_depth',
                'rock_code',
                'rock_code_confidence',
                'rock_name',
                'description',
                'colour',
                'grain_size',
                'logged_by',
                'logged_date',
            ]);

            $query = DB::table('silver.lithology as l')
                ->join('silver.collars as c', 'l.collar_id', '=', 'c.collar_id')
                ->where('c.project_id', $projectId)
                ->select([
                    'l.id',
                    'l.collar_id',
                    'c.hole_id',
                    'l.from_depth',
                    'l.to_depth',
                    'l.rock_code',
                    'l.rock_code_confidence',
                    'l.rock_name',
                    'l.description',
                    'l.colour',
                    'l.grain_size',
                    'l.logged_by',
                    'l.logged_date',
                ]);

            if (! empty($filters['hole_id'])) {
                $query->where('c.hole_id', $filters['hole_id']);
            }
            if (isset($filters['min_confidence'])) {
                $minConf = (float) $filters['min_confidence'];
                // Always include exact-match rows (confidence = 1.0). The
                // filter excludes only NULL (no match at all) and rows
                // below the floor.
                $query->where('l.rock_code_confidence', '>=', $minConf);
            }

            $query->orderBy('c.hole_id')
                ->orderBy('l.from_depth')
                ->chunk(2000, function ($rows) use ($handle) {
                    foreach ($rows as $row) {
                        fputcsv($handle, [
                            $row->id,
                            $row->collar_id,
                            $row->hole_id,
                            $row->from_depth,
                            $row->to_depth,
                            $row->rock_code,
                            $row->rock_code_confidence,
                            $row->rock_name,
                            $row->description,
                            $row->colour,
                            $row->grain_size,
                            $row->logged_by,
                            $row->logged_date,
                        ]);
                    }
                });
        } finally {
            fclose($handle);
        }

        return ['path' => $tmpPath, 'size' => filesize($tmpPath)];
    }
}
