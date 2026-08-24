<?php

declare(strict_types=1);

namespace App\Services\Ingestion;

/**
 * Pick the ingestion route, and the sheet-type hint, for an uploaded drill file.
 *
 * Was `App\Services\Dagster\DrillAssetSelector`, returning Dagster asset keys
 * (`silver_collars`, `silver_xlsx`, …). Dagster has been dormant since
 * 2026-07-28 and CSV/XLSX moved to the `ingest_tabular` Hatchet workflow on
 * 2026-08-20, so the asset keys named nothing that runs. The filename
 * heuristic itself is unchanged and still earns its keep: it is the ONLY
 * hint this upload surface has.
 *
 * That matters because the two surfaces get their hint from different places.
 * `UploadController` takes an explicit category from the import wizard's
 * picker and maps it to `sheet_type`. `DrillUploadController` has no picker —
 * the caller posts a file and nothing else — so without the filename there is
 * nothing to pass, and `ingest_tabular` has to classify from the header row
 * alone. A hint is not a decision: `ingest_tabular` may still reclassify.
 *
 * v1 is filename-only. Slice 2 will peek at the file (CSV header row, sheet
 * classifier for `.xlsx`) and emit a per-sheet dispatch.
 */
final class DrillFileRouter
{
    /**
     * @return array{sheet_type: ?string, route: 'hatchet_tabular'|'fastapi_pdf'|'unrouted'}
     */
    public static function select(string $extension, string $originalFilename): array
    {
        $ext = strtolower($extension);
        $base = strtolower(pathinfo($originalFilename, PATHINFO_FILENAME));

        if ($ext === 'pdf') {
            return ['sheet_type' => null, 'route' => 'fastapi_pdf'];
        }

        if ($ext === 'xlsx' || $ext === 'xls') {
            // No sheet_type on purpose, matching UploadController's `excel`
            // category: a workbook holds several tables, and pinning one type
            // would make ingest_tabular treat every sheet as that type
            // instead of classifying each on its own.
            return ['sheet_type' => null, 'route' => 'hatchet_tabular'];
        }

        if ($ext === 'csv') {
            // First-match wins; ordering matters because 'samples' shows up
            // in some collar filenames as 'sampling_locations.csv'.
            //
            // Boundaries use letter-only lookarounds `(?<![a-z])…(?![a-z])`
            // rather than `\b`: PCRE treats `_` as a word character, so `\b`
            // would NOT fire between a keyword and a trailing `_` (e.g.
            // `collars_2024`, `deviation_shots`, `samples_2024`). Digits,
            // underscores, and string ends must all count as separators.
            //
            // The four values are ingest_tabular's sheet types, and are the
            // same strings UploadController::dispatchGeologyIngest() maps its
            // categories onto — one vocabulary, two ways of arriving at it.
            if (preg_match('/(?<![a-z])(collar|hole|drillhole)s?(?![a-z])/', $base) === 1) {
                return ['sheet_type' => 'collar', 'route' => 'hatchet_tabular'];
            }
            if (preg_match('/(?<![a-z])(litho|geology|rock)/', $base) === 1) {
                return ['sheet_type' => 'lithology', 'route' => 'hatchet_tabular'];
            }
            if (preg_match('/(?<![a-z])(survey|deviation)s?(?![a-z])/', $base) === 1) {
                return ['sheet_type' => 'survey', 'route' => 'hatchet_tabular'];
            }
            if (preg_match('/(?<![a-z])(sample|assay|geochem)s?(?![a-z])/', $base) === 1) {
                return ['sheet_type' => 'sample', 'route' => 'hatchet_tabular'];
            }

            // A CSV with no naming hint is still ingested — ingest_tabular's
            // classifier reads the header row. Previously this returned
            // 'unrouted' and the file was stored and never processed, which
            // is the failure the whole category-mapping design exists to
            // prevent.
            return ['sheet_type' => null, 'route' => 'hatchet_tabular'];
        }

        return ['sheet_type' => null, 'route' => 'unrouted'];
    }
}
