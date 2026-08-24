<?php

declare(strict_types=1);

namespace Tests\Unit\Services\Ingestion;

use App\Services\Ingestion\DrillFileRouter;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\Attributes\Test;
use PHPUnit\Framework\TestCase;

/**
 * CC-01 Item 1 Slice 1 — filename-heuristic routing for drill uploads.
 *
 * No DB / no HTTP. Just exercises the static heuristic so a regression in
 * keyword matching is caught fast.
 *
 * Was `DrillAssetSelectorTest`, asserting Dagster asset keys against a
 * `dagster` route. Those keys named assets that have not run since
 * 2026-07-28, and the controller rejected every CSV and XLSX with a 422
 * before the selector was consulted at all — so these cases were green
 * against code nothing could reach. The heuristic is unchanged; what it
 * returns is now the sheet-type vocabulary `ingest_tabular` actually reads.
 */
class DrillFileRouterTest extends TestCase
{
    #[DataProvider('csvCases')]
    public function test_csv_filename_dispatch(string $name, ?string $expectedSheetType): void
    {
        $result = DrillFileRouter::select('csv', $name);

        $this->assertSame($expectedSheetType, $result['sheet_type']);
        $this->assertSame('hatchet_tabular', $result['route']);
    }

    /** @return array<string, array{0: string, 1: ?string}> */
    public static function csvCases(): array
    {
        return [
            'collar plain' => ['collar.csv', 'collar'],
            'collar plural' => ['collars_2024.csv', 'collar'],
            'drillhole alias' => ['drillholes_q1.csv', 'collar'],
            'lithology' => ['lithology_log.csv', 'lithology'],
            'geology alias' => ['geology_codes.csv', 'lithology'],
            'survey' => ['surveys.csv', 'survey'],
            'deviation alias' => ['deviation_shots.csv', 'survey'],
            'sample' => ['samples_2024.csv', 'sample'],
            'assay alias' => ['assay_results.csv', 'sample'],
            'geochem alias' => ['geochem_data.csv', 'sample'],
            // Still dispatched. ingest_tabular classifies from the header
            // row when there is no filename hint; the old behaviour was to
            // return 'unrouted', store the file and never process it.
            'no hint' => ['data.csv', null],
        ];
    }

    #[Test]
    public function xlsx_dispatches_without_a_sheet_type_hint(): void
    {
        // A workbook holds several tables. Pinning one type would make
        // ingest_tabular apply it to every sheet instead of classifying
        // each — the same reason UploadController excludes its `excel`
        // category from the sheet_type mapping.
        $result = DrillFileRouter::select('xlsx', 'any_workbook.xlsx');

        $this->assertNull($result['sheet_type']);
        $this->assertSame('hatchet_tabular', $result['route']);
    }

    #[Test]
    public function xls_legacy_excel_routes_same_as_xlsx(): void
    {
        $result = DrillFileRouter::select('xls', 'old_workbook.xls');

        $this->assertNull($result['sheet_type']);
        $this->assertSame('hatchet_tabular', $result['route']);
    }

    #[Test]
    public function pdf_routes_to_the_fastapi_bridge(): void
    {
        $result = DrillFileRouter::select('pdf', 'NI43-101_2024.pdf');

        $this->assertNull($result['sheet_type']);
        $this->assertSame('fastapi_pdf', $result['route']);
    }

    #[Test]
    public function an_extension_with_no_workflow_is_unrouted(): void
    {
        // `unrouted` must stay reachable. It is what stops the controller
        // claiming a dispatch it never made — LAS has a workflow
        // (ingest_well_logs) but this surface does not accept it.
        $result = DrillFileRouter::select('las', 'gamma_log.las');

        $this->assertNull($result['sheet_type']);
        $this->assertSame('unrouted', $result['route']);
    }

    #[Test]
    public function matching_is_case_insensitive(): void
    {
        $result = DrillFileRouter::select('CSV', 'COLLARS_2024.CSV');

        $this->assertSame('collar', $result['sheet_type']);
        $this->assertSame('hatchet_tabular', $result['route']);
    }

    #[Test]
    public function every_sheet_type_it_emits_is_one_ingest_tabular_knows(): void
    {
        // The hint is passed straight through to
        // /internal/v1/shadow/ingest_tabular/trigger. A value outside this
        // set would be silently ignored there, and the file would be
        // classified from its header row as though no hint were sent —
        // a fallback that looks like success.
        $known = ['collar', 'survey', 'lithology', 'sample'];

        foreach (self::csvCases() as $case) {
            [$name, $expected] = $case;
            if ($expected === null) {
                continue;
            }
            $this->assertContains(
                $expected,
                $known,
                "{$name} routes to sheet_type '{$expected}', which "
                .'ingest_tabular does not recognise',
            );
        }
    }
}
