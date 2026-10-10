<?php

declare(strict_types=1);

namespace App\Services\Exports;

use App\Models\Sample;
use App\Models\Survey;
use Illuminate\Support\Facades\Log;

/**
 * Exports a Micromine / Leapfrog compatible drill-hole data bundle.
 *
 * Produces a ZIP archive containing three CSVs:
 *   - collars.csv  — hole_id, easting, northing, elevation, total_depth, azimuth, dip, epsg
 *   - surveys.csv  — hole_id, depth, azimuth, dip
 *   - assays.csv   — hole_id, from_depth, to_depth, sample_type, u3o8_ppm, au_ppb, cu_pct
 *
 * These column names are what Leapfrog and Micromine expect for their standard
 * drill hole import wizard.
 *
 * collars.csv easting / northing are the collar's position in the CRS named by
 * the trailing `epsg` column (the project's projected CRS, else the collar's UTM
 * zone), not the uploaded source values, which carry no CRS; see
 * CollarExportQuery.
 *
 * assays.csv takes each grade from silver.samples.commodity_assays by element,
 * whatever the case or unit it was stored in, converted to the column's unit; see
 * CommodityAssayValue.
 *
 * Collars are streamed, and surveys and samples are read a page at a time, so a
 * project's size does not decide the worker's memory use. The only thing held is
 * a hole_id per collar, to label child rows.
 *
 * Returns array{path: string, size: int}.
 */
class CsaBundleExporter
{
    /** Rows per page when reading child tables. */
    private const PAGE_SIZE = 2000;

    /**
     * @param array<string, mixed> $filters
     *
     * @return array{path: string, size: int}
     */
    public function export(string $projectId, array $filters = []): array
    {
        $zipPath = sys_get_temp_dir().'/georag_csa_bundle_'.uniqid().'.zip';

        try {
            $unplaced = $this->writeBundle($projectId, $filters, $zipPath);
        } catch (\Throwable $e) {
            // The caller is never told the path of a bundle that failed.
            @unlink($zipPath);

            throw $e;
        }

        if ($unplaced > 0) {
            Log::warning('csa_bundle export: collars with no geom_4326 were written without coordinates', [
                'project_id' => $projectId,
                'collars' => $unplaced,
            ]);
        }

        return [
            'path' => $zipPath,
            'size' => filesize($zipPath),
        ];
    }

    // -------------------------------------------------------------------------
    // Private helpers
    // -------------------------------------------------------------------------

    /**
     * Write the three CSVs to temp files, then bundle them into the ZIP.
     *
     * @param array<string, mixed> $filters
     *
     * @return int collars written without coordinates (no geom_4326)
     */
    private function writeBundle(string $projectId, array $filters, string $zipPath): int
    {
        $tmpDir = sys_get_temp_dir();

        // collar_id => hole_id, filled while collars.csv is written, so the
        // surveys and assays can name their hole without a join per page.
        $holeIdByCollar = [];
        $unplaced = 0;

        // zip entry name => temp file, removed whether or not the bundle is built.
        $csvFiles = [];

        try {
            $csvFiles['collars.csv'] = $this->writeCsvFile($tmpDir, 'collars', function ($handle) use ($projectId, $filters, &$holeIdByCollar, &$unplaced): void {
                fputcsv($handle, ['hole_id', 'easting', 'northing', 'elevation', 'total_depth', 'azimuth', 'dip', 'epsg'], escape: '');

                $collars = CollarExportQuery::forProject($projectId, $filters)
                    ->orderBy('hole_id')
                    ->orderBy('collar_id')
                    ->cursor();

                foreach ($collars as $c) {
                    $holeIdByCollar[$c->collar_id] = $c->hole_id;
                    if ($c->export_epsg === null) {
                        $unplaced++;
                    }

                    fputcsv($handle, [
                        $c->hole_id,
                        CollarExportQuery::coordinate($c->export_easting),
                        CollarExportQuery::coordinate($c->export_northing),
                        $c->elevation,
                        $c->total_depth,
                        $c->azimuth,
                        $c->dip,
                        $c->export_epsg,
                    ], escape: '');
                }
            });

            // The children of exactly the collars that passed the filters, as a
            // subquery: a list of ids overflows Postgres' bind-parameter limit
            // on a project of more than ~65,000 collars.
            $collarIds = CollarExportQuery::ids($projectId, $filters);

            $csvFiles['surveys.csv'] = $this->writeCsvFile($tmpDir, 'surveys', function ($handle) use ($collarIds, &$holeIdByCollar): void {
                fputcsv($handle, ['hole_id', 'depth', 'azimuth', 'dip'], escape: '');

                Survey::query()
                    ->whereIn('collar_id', $collarIds)
                    ->orderBy('collar_id')
                    ->orderBy('depth')
                    // Offset paging is only stable over a total order.
                    ->orderBy('survey_id')
                    ->chunk(self::PAGE_SIZE, function ($surveys) use ($handle, &$holeIdByCollar): void {
                        foreach ($surveys as $s) {
                            fputcsv($handle, [
                                $holeIdByCollar[$s->collar_id] ?? $s->collar_id,
                                $s->depth,
                                $s->azimuth,
                                $s->dip,
                            ], escape: '');
                        }
                    });
            });

            $csvFiles['assays.csv'] = $this->writeCsvFile($tmpDir, 'assays', function ($handle) use ($collarIds, &$holeIdByCollar): void {
                fputcsv($handle, ['hole_id', 'from_depth', 'to_depth', 'sample_type', 'u3o8_ppm', 'au_ppb', 'cu_pct'], escape: '');

                Sample::query()
                    ->whereIn('collar_id', $collarIds)
                    ->orderBy('collar_id')
                    ->orderBy('from_depth')
                    ->orderBy('sample_id')
                    ->chunk(self::PAGE_SIZE, function ($samples) use ($handle, &$holeIdByCollar): void {
                        foreach ($samples as $sample) {
                            $assays = is_array($sample->commodity_assays) ? $sample->commodity_assays : [];

                            fputcsv($handle, [
                                $holeIdByCollar[$sample->collar_id] ?? $sample->collar_id,
                                $sample->from_depth,
                                $sample->to_depth,
                                $sample->sample_type,
                                CommodityAssayValue::in($assays, 'U3O8', 'ppm'),
                                CommodityAssayValue::in($assays, 'Au', 'ppb'),
                                CommodityAssayValue::in($assays, 'Cu', 'pct'),
                            ], escape: '');
                        }
                    });
            });

            ZipBundle::write($zipPath, $csvFiles);
        } finally {
            foreach ($csvFiles as $path) {
                @unlink($path);
            }
        }

        return $unplaced;
    }

    /**
     * Write rows to a uniquely named temp CSV and return its path. A writer
     * that throws leaves no file behind.
     *
     * @param string $name Name hint (used in the filename for debuggability).
     * @param callable $writer Receives an open file handle.
     */
    private function writeCsvFile(string $dir, string $name, callable $writer): string
    {
        $path = $dir.'/georag_csa_'.$name.'_'.uniqid().'.csv';
        $handle = fopen($path, 'w');

        if ($handle === false) {
            throw new \RuntimeException("Cannot open temp CSV for writing: {$path}");
        }

        try {
            $writer($handle);
        } catch (\Throwable $e) {
            @unlink($path);

            throw $e;
        } finally {
            fclose($handle);
        }

        return $path;
    }
}
