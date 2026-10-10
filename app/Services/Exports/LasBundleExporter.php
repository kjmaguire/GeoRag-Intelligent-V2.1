<?php

declare(strict_types=1);

namespace App\Services\Exports;

use App\Models\Collar;
use App\Models\WellLogCurve;
use Illuminate\Support\Collection;
use Illuminate\Support\Facades\Log;

/**
 * Exports well-log data as LAS 2.0 files bundled into a ZIP archive.
 *
 * One LAS file is produced per collar that has curves in silver.well_log_curves.
 * Collars with no curves are silently skipped. If only one collar has curves,
 * the ZIP still wraps it for consistency.
 *
 * Each file's ~WELL section names the collar's position in a stated CRS:
 * `LOC` carries easting/northing and `HZCS` the EPSG code they are in (the
 * project's projected CRS, else the collar's UTM zone), derived from
 * silver.collars.geom_4326 rather than the stored easting/northing, which carry
 * whatever CRS and unit the upload used; see CollarExportQuery.
 *
 * Collars are taken one at a time, and only those that have curves, so the
 * worker holds one hole's depth and value arrays at a time however large the
 * project is. File names inside the ZIP are made from the hole id with
 * everything outside [A-Za-z0-9._-] replaced, because a real hole id
 * ("LEB 23/001") is a path separator and a space away from a broken or
 * traversing entry name.
 *
 * LAS 2.0 format reference: https://www.cwls.org/products/#products-las
 *
 * Returns array{path: string, size: int}.
 */
class LasBundleExporter
{
    /** Longest file stem taken from a hole id; the rest is dropped. */
    private const MAX_STEM_LENGTH = 80;

    /**
     * @param array<string, mixed> $filters
     *
     * @return array{path: string, size: int}
     */
    public function export(string $projectId, array $filters = []): array
    {
        $zipPath = sys_get_temp_dir().'/georag_las_bundle_'.uniqid().'.zip';

        try {
            $unplaced = $this->writeBundle($projectId, $filters, $zipPath);
        } catch (\Throwable $e) {
            // The caller is never told the path of a bundle that failed.
            @unlink($zipPath);

            throw $e;
        }

        if ($unplaced > 0) {
            Log::warning('las_bundle export: collars with no geom_4326 were written with an unknown location', [
                'project_id' => $projectId,
                'collars' => $unplaced,
            ]);
        }

        return [
            'path' => $zipPath,
            'size' => filesize($zipPath),
        ];
    }

    /**
     * @param array<string, mixed> $filters
     *
     * @return int collars written without a location (no geom_4326)
     */
    private function writeBundle(string $projectId, array $filters, string $zipPath): int
    {
        $tmpDir = sys_get_temp_dir();

        // zip entry name => temp file. ZipArchive reads each file when the
        // archive is closed, so they live until ZipBundle::write returns.
        $entries = [];
        $entryNames = [];
        $unplaced = 0;

        try {
            $collars = CollarExportQuery::forProject($projectId, $filters)
                // Only collars that have a curve: a project is mostly holes
                // with none, and each would cost a query to find that out.
                ->whereExists(static function ($curves): void {
                    $curves->selectRaw('1')
                        ->from('silver.well_log_curves as w')
                        ->whereColumn('w.collar_id', 'silver.collars.collar_id');
                })
                ->orderBy('hole_id')
                ->orderBy('collar_id')
                ->cursor();

            foreach ($collars as $collar) {
                $curves = WellLogCurve::where('collar_id', $collar->collar_id)
                    ->orderBy('curve_name')
                    ->get();

                if ($curves->isEmpty()) {
                    continue;
                }

                if ($collar->export_epsg === null) {
                    $unplaced++;
                }

                $entries[$this->entryName((string) $collar->hole_id, $entryNames)] = $this->writeTempFile(
                    $tmpDir,
                    $this->buildLas2($collar, $curves),
                );
            }

            // If no curves were found for any collar, add a notice file.
            if ($entries === []) {
                $entries['README.txt'] = $this->writeTempFile($tmpDir, $this->noCurvesNotice($projectId));
            }

            ZipBundle::write($zipPath, $entries);
        } finally {
            foreach ($entries as $path) {
                @unlink($path);
            }
        }

        return $unplaced;
    }

    private function writeTempFile(string $dir, string $contents): string
    {
        $path = tempnam($dir, 'georag_las_');
        if ($path === false) {
            throw new \RuntimeException("Cannot create a temp file in {$dir}");
        }

        file_put_contents($path, $contents);

        return $path;
    }

    /**
     * A name for the ZIP entry that is safe on every filesystem it may be
     * extracted to, unique within the bundle.
     *
     * Hole ids are free text: "LEB 23/001" would otherwise become a directory,
     * and "../x" an entry that escapes the extraction folder. Anything outside
     * [A-Za-z0-9._-] becomes "_", leading and trailing separators go, and a
     * clash (two ids that differ only in the characters replaced, or only in
     * case, which Windows and macOS fold) is numbered rather than overwritten.
     *
     * @param array<string, true> $used lower-cased entry names handed out so far
     */
    private function entryName(string $holeId, array &$used): string
    {
        $stem = trim((string) preg_replace('/[^A-Za-z0-9._-]+/', '_', $holeId), '._-');
        $stem = rtrim(substr($stem, 0, self::MAX_STEM_LENGTH), '._-');
        if ($stem === '') {
            $stem = 'hole';
        }

        $name = "{$stem}.las";
        for ($n = 2; isset($used[strtolower($name)]); $n++) {
            $name = "{$stem}-{$n}.las";
        }
        $used[strtolower($name)] = true;

        return $name;
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
        if ($collar->export_epsg !== null) {
            $lines[] = sprintf(
                'LOC .                  E%.2f N%.2f : Location (Easting Northing)',
                $collar->export_easting,
                $collar->export_northing,
            );
            $lines[] = sprintf('HZCS.                  EPSG:%d : Horizontal coordinate system', $collar->export_epsg);
        } else {
            $lines[] = 'LOC .                  UNKNOWN : Location (collar has no recorded position)';
        }
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
