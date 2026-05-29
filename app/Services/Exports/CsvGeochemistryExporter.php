<?php

declare(strict_types=1);

namespace App\Services\Exports;

use Illuminate\Support\Facades\DB;

/**
 * Exports silver.geochemistry rows for a project as a plain CSV file.
 *
 * Includes the major-oxide whole-rock columns and the derived
 * petrochemistry indices (mg_number, CIA, eu_anomaly) but NOT the
 * full REE JSON blob — a CSV with a nested JSON column round-trips
 * badly in most consumer tools. Anyone who needs REEs should pull
 * silver.geochemistry directly or use a future GeoParquet export.
 */
class CsvGeochemistryExporter
{
    /**
     * @param string $projectId UUID of the parent project.
     * @param array<string,mixed> $filters
     *                                     hole_id, sample_type, include_ree (bool — emits ree_json as a string column)
     *
     * @return array{path: string, size: int}
     */
    public function export(string $projectId, array $filters = []): array
    {
        $tmpPath = sys_get_temp_dir().'/georag_geochem_'.uniqid().'.csv';
        $handle = fopen($tmpPath, 'w');
        if ($handle === false) {
            throw new \RuntimeException("Cannot open temp file for writing: {$tmpPath}");
        }

        $includeRee = ! empty($filters['include_ree']);

        try {
            $header = [
                'geochem_id',
                'collar_id',
                'hole_id',
                'from_depth',
                'to_depth',
                'sample_id',
                'sample_type',
                'sio2_wt_pct',
                'al2o3_wt_pct',
                'fe2o3_wt_pct',
                'mgo_wt_pct',
                'cao_wt_pct',
                'na2o_wt_pct',
                'k2o_wt_pct',
                'mg_number',
                'cia',
                'eu_anomaly',
            ];
            if ($includeRee) {
                $header[] = 'ree_json';
            }
            fputcsv($handle, $header);

            $selectCols = [
                'g.geochem_id',
                'g.collar_id',
                'c.hole_id',
                'g.from_depth',
                'g.to_depth',
                'g.sample_id',
                'g.sample_type',
                'g.sio2_wt_pct',
                'g.al2o3_wt_pct',
                'g.fe2o3_wt_pct',
                'g.mgo_wt_pct',
                'g.cao_wt_pct',
                'g.na2o_wt_pct',
                'g.k2o_wt_pct',
                'g.mg_number',
                'g.cia',
                'g.eu_anomaly',
            ];
            if ($includeRee) {
                $selectCols[] = 'g.ree_json';
            }

            $query = DB::table('silver.geochemistry as g')
                ->join('silver.collars as c', 'g.collar_id', '=', 'c.collar_id')
                ->where('c.project_id', $projectId)
                ->select($selectCols);

            if (! empty($filters['hole_id'])) {
                $query->where('c.hole_id', $filters['hole_id']);
            }
            if (! empty($filters['sample_type'])) {
                $query->where('g.sample_type', $filters['sample_type']);
            }

            $query->orderBy('c.hole_id')
                ->orderBy('g.from_depth')
                ->chunk(2000, function ($rows) use ($handle, $includeRee) {
                    foreach ($rows as $row) {
                        $line = [
                            $row->geochem_id,
                            $row->collar_id,
                            $row->hole_id,
                            $row->from_depth,
                            $row->to_depth,
                            $row->sample_id,
                            $row->sample_type,
                            $row->sio2_wt_pct,
                            $row->al2o3_wt_pct,
                            $row->fe2o3_wt_pct,
                            $row->mgo_wt_pct,
                            $row->cao_wt_pct,
                            $row->na2o_wt_pct,
                            $row->k2o_wt_pct,
                            $row->mg_number,
                            $row->cia,
                            $row->eu_anomaly,
                        ];
                        if ($includeRee) {
                            // ree_json comes back as a JSON-encoded string;
                            // pass through verbatim so consumers can re-parse.
                            $line[] = $row->ree_json;
                        }
                        fputcsv($handle, $line);
                    }
                });
        } finally {
            fclose($handle);
        }

        return ['path' => $tmpPath, 'size' => filesize($tmpPath)];
    }
}
