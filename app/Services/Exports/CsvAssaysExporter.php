<?php

declare(strict_types=1);

namespace App\Services\Exports;

use Illuminate\Support\Facades\DB;

/**
 * Exports silver.assays_v2 rows for a project as a plain CSV file.
 *
 * Joins assays_v2 → collars to scope by project_id, and emits one row per
 * (sample, element). QC flag and below-detection state are preserved so
 * downstream Python consumers can filter on them.
 *
 * For Shaun's prospectivity workflow this is the authoritative bulk
 * source — the per-hole analysis bundle endpoint returns aggregated
 * structures and is not designed for project-wide assay dumps.
 *
 * ## Repointed 2026-08-28
 *
 * This used to read `silver.assays` JOIN `silver.samples` JOIN
 * `silver.collars`. Only two of those three exist: `silver.assays` was
 * declared solely in `database/raw/phase0/109`, which CD never applies and
 * which the architecture doc does not name at all, so every export of this
 * kind failed on Azure with `42P01 undefined_table`. `silver.samples` does
 * exist (migration 2026_04_09_180600) but is no longer needed — `assays_v2`
 * carries `collar_id`, `from_depth` and `to_depth` itself, so the join is
 * one hop shorter.
 *
 * The CSV header is deliberately UNCHANGED. Downstream Python consumers read
 * these column names, so the v2 columns are aliased back to the old names
 * rather than renaming the output: `element`→`assay_element`,
 * `value`→`assay_value`, `unit`→`assay_unit`,
 * `analysis_method`→`method_code`, `under_detection`→`below_detection`,
 * `qaqc_flag`→`qc_flag`, `id`→`assay_id`.
 *
 * One behavioural subtlety, deliberately handled: the legacy columns were
 * `NOT NULL`, so `!= 'rejected'` and `= false` were safe. The `assays_v2`
 * equivalents are nullable (`qaqc_flag text DEFAULT 'pass'`,
 * `under_detection boolean DEFAULT false`), where those same predicates
 * evaluate to NULL and silently drop every unflagged row. Both filters
 * therefore use `IS DISTINCT FROM`, which keeps NULLs.
 *
 * NOTE for the SME: `assays_v2.qaqc_flag` carries no CHECK constraint and no
 * vocabulary is defined anywhere in the codebase — only the `'pass'` default.
 * The `exclude_rejected` filter keeps the literal `'rejected'` the legacy
 * column used, which is behaviour-preserving for callers but will need
 * confirming once QA/QC values are actually written.
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

            $query = DB::table('silver.assays_v2 as a')
                ->join('silver.collars as c', 'a.collar_id', '=', 'c.collar_id')
                ->where('c.project_id', $projectId)
                ->select([
                    'a.id as assay_id',
                    'a.sample_id',
                    'a.collar_id',
                    'c.hole_id',
                    'a.from_depth',
                    'a.to_depth',
                    'a.element as assay_element',
                    'a.value as assay_value',
                    'a.unit as assay_unit',
                    'a.analysis_method as method_code',
                    'a.detection_limit',
                    'a.under_detection as below_detection',
                    'a.qaqc_flag as qc_flag',
                ]);

            if (! empty($filters['hole_id'])) {
                $query->where('c.hole_id', $filters['hole_id']);
            }
            if (! empty($filters['element'])) {
                $query->where('a.element', $filters['element']);
            }
            // IS DISTINCT FROM, not !=: qaqc_flag is nullable on assays_v2
            // (the legacy qc_flag was NOT NULL), and `!= 'rejected'` is NULL
            // for an unflagged row, which would drop it from the export.
            if (! empty($filters['exclude_rejected'])) {
                $query->whereRaw("a.qaqc_flag IS DISTINCT FROM 'rejected'");
            }
            // Same nullability reasoning for under_detection.
            if (isset($filters['include_below_detection']) && $filters['include_below_detection'] === false) {
                $query->whereRaw('a.under_detection IS DISTINCT FROM true');
            }

            $query->orderBy('c.hole_id')
                ->orderBy('a.from_depth')
                ->orderBy('a.element')
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
