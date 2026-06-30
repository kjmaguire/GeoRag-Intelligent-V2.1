<?php

declare(strict_types=1);

namespace App\Services\Exports;

use App\Models\Collar;
use App\Models\WellLogCurve;
use Illuminate\Support\Collection;
use ZipArchive;

/**
 * Exports well-log data as LAS 2.0 files bundled into a ZIP archive.
 *
 * One LAS file is produced per collar that has curves in silver.well_log_curves.
 * Collars with no curves are silently skipped. If only one collar has curves,
 * the ZIP still wraps it for consistency.
 *
 * LAS 2.0 format reference: https://www.cwls.org/products/#products-las
 *
 * Returns array{path: string, size: int}.
 */
class LasBundleExporter
{
    /**
     * @return array{path: string, size: int}
     */
    public function export(string $projectId, array $filters = []): array
    {
        $collars = $this->fetchCollars($projectId, $filters);
        $collarIds = $collars->pluck('collar_id')->all();

        // Eager-load all curves grouped by collar_id.
        $curvesByCollar = WellLogCurve::whereIn('collar_id', $collarIds)
            ->orderBy('collar_id')
            ->orderBy('curve_name')
            ->get()
            ->groupBy('collar_id');

        $tmpDir = sys_get_temp_dir();
        $zipPath = $tmpDir.'/georag_las_bundle_'.uniqid().'.zip';

        $zip = new ZipArchive;
        if ($zip->open($zipPath, ZipArchive::CREATE | ZipArchive::OVERWRITE) !== true) {
            throw new \RuntimeException("Cannot create ZIP archive at: {$zipPath}");
        }

        $lasFiles = [];
        $collarCount = 0;

        try {
            foreach ($collars as $collar) {
                $curves = $curvesByCollar->get($collar->collar_id);

                if (! $curves || $curves->isEmpty()) {
                    continue;
                }

                $lasContent = $this->buildLas2($collar, $curves);
                $lasPath = $tmpDir.'/georag_'.$collar->hole_id.'_'.uniqid().'.las';
                file_put_contents($lasPath, $lasContent);

                $lasFiles[] = $lasPath;
                $zip->addFile($lasPath, $collar->hole_id.'.las');
                $collarCount++;
            }

            // If no curves were found for any collar, add a notice file.
            if ($collarCount === 0) {
                $noticePath = $tmpDir.'/georag_las_no_curves_'.uniqid().'.txt';
                file_put_contents($noticePath, $this->noCurvesNotice($projectId));
                $lasFiles[] = $noticePath;
                $zip->addFile($noticePath, 'README.txt');
            }

            $zip->close();
        } finally {
            foreach ($lasFiles as $f) {
                @unlink($f);
            }
        }

        return [
            'path' => $zipPath,
            'size' => filesize($zipPath),
        ];
    }

    // -------------------------------------------------------------------------
    // LAS 2.0 builder
    // -------------------------------------------------------------------------

    /**
     * Build the text content of a LAS 2.0 file for a single collar.
     *
     * @param Collection $curves All WellLogCurve rows for this collar.
     */
    private function buildLas2(Collar $collar, Collection $curves): string
    {
        // Use the first curve's metadata for the file-level section.
        $firstCurve = $curves->first();
        $step = $firstCurve->step ?? 0.1;
        $nullValue = $firstCurve->null_value ?? -999.25;
        $lasVersion = $firstCurve->las_version ?? '2.0';

        $lines = [];

        // ---- ~VERSION section ----
        $lines[] = '~VERSION INFORMATION';
        $lines[] = sprintf('VERS.                  %s : LAS Format Version', $lasVersion);
        $lines[] = 'WRAP.                  NO  : One line per depth step';
        $lines[] = '';

        // ---- ~WELL section ----
        $lines[] = '~WELL INFORMATION';
        $lines[] = sprintf('STRT.M                 %.4f : Start depth', $firstCurve->min_depth);
        $lines[] = sprintf('STOP.M                 %.4f : Stop depth', $firstCurve->max_depth);
        $lines[] = sprintf('STEP.M                 %.4f : Depth increment', $step);
        $lines[] = sprintf('NULL.                  %.2f : Null value', $nullValue);
        $lines[] = sprintf('COMP.                  GeoRAG : Company');
        $lines[] = sprintf('WELL.                  %s : Well name', $collar->hole_id);
        $lines[] = sprintf('FLD .                  %s : Field', $collar->project_id);
        $lines[] = sprintf('LOC .                  E%.2f N%.2f : Location (Easting Northing)', $collar->easting, $collar->northing);
        $lines[] = sprintf('ELEV.M                 %.2f : Elevation', $collar->elevation ?? 0.0);
        $lines[] = sprintf('DATE.                  %s : Export date', now()->format('Y-m-d'));
        $lines[] = '';

        // ---- ~CURVE section ----
        $lines[] = '~CURVE INFORMATION';
        $lines[] = 'DEPT.M                  : Depth';

        foreach ($curves as $curve) {
            $unit = $curve->curve_unit ?? '';
            $desc = $curve->curve_description ?? $curve->curve_name;
            $lines[] = sprintf('%-20s%-20s: %s', $curve->curve_name.'.'.$unit, '', $desc);
        }

        $lines[] = '';

        // ---- ~A (ASCII data) section ----
        $lines[] = '~ASCII LOG DATA';

        // Zip depths from the first curve (all curves for same collar share depths).
        // Depths are stored as PostgreSQL DOUBLE PRECISION[] — retrieved as comma-separated string
        // or already as PHP array depending on the driver. Handle both.
        $depths = $this->parsePostgresArray($firstCurve->depths);

        // Build value columns for each depth index.
        $curveValues = [];
        foreach ($curves as $curve) {
            $curveValues[] = $this->parsePostgresArray($curve->values);
        }

        foreach ($depths as $i => $depth) {
            $row = [sprintf('%.4f', $depth)];
            foreach ($curveValues as $vals) {
                $v = $vals[$i] ?? $nullValue;
                $row[] = sprintf('%.4f', is_numeric($v) ? $v : $nullValue);
            }
            $lines[] = implode('    ', $row);
        }

        return implode("\n", $lines)."\n";
    }

    /**
     * Parse a PostgreSQL array literal like "{1.0,2.5,3.0}" into a PHP array.
     * If already a PHP array (e.g. when Eloquent casting is active), pass it through.
     *
     * @param string|array $value
     *
     * @return array<int, float>
     */
    private function parsePostgresArray(mixed $value): array
    {
        if (is_array($value)) {
            return array_map('floatval', $value);
        }

        if (! is_string($value)) {
            return [];
        }

        // Strip leading/trailing braces and split on comma.
        $trimmed = trim($value, '{}');

        if ($trimmed === '') {
            return [];
        }

        return array_map('floatval', explode(',', $trimmed));
    }

    // -------------------------------------------------------------------------
    // Data fetchers
    // -------------------------------------------------------------------------

    private function fetchCollars(string $projectId, array $filters): Collection
    {
        $query = Collar::where('project_id', $projectId);

        if (! empty($filters['hole_type'])) {
            $query->where('hole_type', $filters['hole_type']);
        }
        if (! empty($filters['status'])) {
            $query->where('status', $filters['status']);
        }
        if (! empty($filters['drill_date_from'])) {
            $query->where('drill_date', '>=', $filters['drill_date_from']);
        }
        if (! empty($filters['drill_date_to'])) {
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

    private function noCurvesNotice(string $projectId): string
    {
        return <<<TEXT
GeoRAG Export — LAS Bundle
===========================

No well-log curves were found for this project's collars (project_id: {$projectId}).

Well-log curve data is ingested from .las source files via the Dagster pipeline.
Once LAS files have been ingested, re-request the LAS bundle export.

TEXT;
    }
}
