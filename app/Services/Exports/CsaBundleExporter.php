<?php

declare(strict_types=1);

namespace App\Services\Exports;

use App\Models\Collar;
use App\Models\Sample;
use App\Models\Survey;
use Illuminate\Support\Collection;
use ZipArchive;

/**
 * Exports a Micromine / Leapfrog compatible drill-hole data bundle.
 *
 * Produces a ZIP archive containing three CSVs:
 *   - collars.csv  — hole_id, easting, northing, elevation, total_depth, azimuth, dip
 *   - surveys.csv  — hole_id, depth, azimuth, dip
 *   - assays.csv   — hole_id, from_depth, to_depth, sample_type, u3o8_ppm, au_ppb, cu_pct
 *
 * These column names are what Leapfrog and Micromine expect for their standard
 * drill hole import wizard.
 *
 * Returns array{path: string, size: int}.
 */
class CsaBundleExporter
{
    /**
     * @param  string  $projectId
     * @param  array   $filters
     * @return array{path: string, size: int}
     */
    public function export(string $projectId, array $filters = []): array
    {
        $collars = $this->fetchCollars($projectId, $filters);
        $collarIds = $collars->pluck('collar_id')->all();

        $surveys = $this->fetchSurveys($collarIds);
        $assays  = $this->fetchAssays($collarIds);

        // Build a hole_id lookup keyed by collar_id so surveys/assays can
        // reference the human-readable hole identifier.
        $holeIdByCollar = $collars->pluck('hole_id', 'collar_id');

        $tmpDir  = sys_get_temp_dir();
        $zipPath = $tmpDir . '/georag_csa_bundle_' . uniqid() . '.zip';

        // Write each CSV to a temp file first, then bundle.
        $collarsCsv = $this->writeCsvFile($tmpDir, 'collars', function ($handle) use ($collars) {
            fputcsv($handle, ['hole_id', 'easting', 'northing', 'elevation', 'total_depth', 'azimuth', 'dip']);
            foreach ($collars as $c) {
                fputcsv($handle, [
                    $c->hole_id,
                    $c->easting,
                    $c->northing,
                    $c->elevation,
                    $c->total_depth,
                    $c->azimuth,
                    $c->dip,
                ]);
            }
        });

        $surveysCsv = $this->writeCsvFile($tmpDir, 'surveys', function ($handle) use ($surveys, $holeIdByCollar) {
            fputcsv($handle, ['hole_id', 'depth', 'azimuth', 'dip']);
            foreach ($surveys as $s) {
                fputcsv($handle, [
                    $holeIdByCollar[$s->collar_id] ?? $s->collar_id,
                    $s->depth,
                    $s->azimuth,
                    $s->dip,
                ]);
            }
        });

        $assaysCsv = $this->writeCsvFile($tmpDir, 'assays', function ($handle) use ($assays, $holeIdByCollar) {
            fputcsv($handle, ['hole_id', 'from_depth', 'to_depth', 'sample_type', 'u3o8_ppm', 'au_ppb', 'cu_pct']);
            foreach ($assays as $sample) {
                $ca       = is_array($sample->commodity_assays) ? $sample->commodity_assays : [];
                $u3o8     = $ca['u3o8_ppm'] ?? null;
                $au       = $ca['au_ppb']   ?? null;
                $cu       = $ca['cu_pct']   ?? null;

                fputcsv($handle, [
                    $holeIdByCollar[$sample->collar_id] ?? $sample->collar_id,
                    $sample->from_depth,
                    $sample->to_depth,
                    $sample->sample_type,
                    $u3o8,
                    $au,
                    $cu,
                ]);
            }
        });

        try {
            $zip = new ZipArchive();
            if ($zip->open($zipPath, ZipArchive::CREATE | ZipArchive::OVERWRITE) !== true) {
                throw new \RuntimeException("Cannot create ZIP archive at: {$zipPath}");
            }

            $zip->addFile($collarsCsv, 'collars.csv');
            $zip->addFile($surveysCsv, 'surveys.csv');
            $zip->addFile($assaysCsv,  'assays.csv');
            $zip->close();
        } finally {
            @unlink($collarsCsv);
            @unlink($surveysCsv);
            @unlink($assaysCsv);
        }

        return [
            'path' => $zipPath,
            'size' => filesize($zipPath),
        ];
    }

    // -------------------------------------------------------------------------
    // Private helpers
    // -------------------------------------------------------------------------

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

    private function fetchSurveys(array $collarIds): Collection
    {
        if (empty($collarIds)) {
            return collect();
        }

        return Survey::whereIn('collar_id', $collarIds)
            ->orderBy('collar_id')
            ->orderBy('depth')
            ->get();
    }

    private function fetchAssays(array $collarIds): Collection
    {
        if (empty($collarIds)) {
            return collect();
        }

        return Sample::whereIn('collar_id', $collarIds)
            ->orderBy('collar_id')
            ->orderBy('from_depth')
            ->get();
    }

    /**
     * Write rows to a uniquely named temp CSV and return its path.
     *
     * @param  string    $dir
     * @param  string    $name   Name hint (used in the filename for debuggability).
     * @param  callable  $writer Receives an open file handle.
     */
    private function writeCsvFile(string $dir, string $name, callable $writer): string
    {
        $path   = $dir . '/georag_csa_' . $name . '_' . uniqid() . '.csv';
        $handle = fopen($path, 'w');

        if ($handle === false) {
            throw new \RuntimeException("Cannot open temp CSV for writing: {$path}");
        }

        try {
            $writer($handle);
        } finally {
            fclose($handle);
        }

        return $path;
    }
}
