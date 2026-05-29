<?php

declare(strict_types=1);

namespace App\Services\Dagster;

/**
 * Pick the Dagster asset key for a freshly-uploaded drill file.
 *
 * v1 of Slice 1 — filename-only heuristic. Slice 2 will refine this by
 * peeking at the file (CSV header row for `.csv`, sheet classifier for
 * `.xlsx`) and emitting a per-sheet dispatch.
 *
 * Returns null when the file is a PDF (caller dispatches to the existing
 * FastAPI ingest_pdf bridge instead) or no heuristic matched.
 */
final class DrillAssetSelector
{
    /**
     * @return array{asset_key: ?string, route: 'dagster'|'fastapi_pdf'|'unrouted'}
     */
    public static function select(string $extension, string $originalFilename): array
    {
        $ext = strtolower($extension);
        $base = strtolower(pathinfo($originalFilename, PATHINFO_FILENAME));

        if ($ext === 'pdf') {
            return ['asset_key' => null, 'route' => 'fastapi_pdf'];
        }

        if ($ext === 'xlsx' || $ext === 'xls') {
            // silver_xlsx already does per-sheet dispatch internally via the
            // _sheet_classifier. Trigger it once and let it fan out.
            return ['asset_key' => 'silver_xlsx', 'route' => 'dagster'];
        }

        if ($ext === 'csv') {
            // First-match wins; ordering matters because 'samples' shows up
            // in some collar filenames as 'sampling_locations.csv'.
            if (preg_match('/\b(collar|hole|drillhole)s?\b/', $base) === 1) {
                return ['asset_key' => 'silver_collars', 'route' => 'dagster'];
            }
            if (preg_match('/\b(litho|geology|rock)/', $base) === 1) {
                return ['asset_key' => 'silver_lithology', 'route' => 'dagster'];
            }
            if (preg_match('/\b(survey|deviation)s?\b/', $base) === 1) {
                return ['asset_key' => 'silver_surveys', 'route' => 'dagster'];
            }
            if (preg_match('/\b(sample|assay|geochem)s?\b/', $base) === 1) {
                return ['asset_key' => 'silver_samples', 'route' => 'dagster'];
            }

            // CSV with no naming hint — let the user know via the SRQ
            // routing_reason rather than guessing. Slice 2 will read the
            // header row and classify properly.
            return ['asset_key' => null, 'route' => 'unrouted'];
        }

        return ['asset_key' => null, 'route' => 'unrouted'];
    }
}
