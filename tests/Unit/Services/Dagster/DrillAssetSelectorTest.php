<?php

declare(strict_types=1);

namespace Tests\Unit\Services\Dagster;

use App\Services\Dagster\DrillAssetSelector;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\TestCase;

/**
 * CC-01 Item 1 Slice 1 — filename-heuristic asset selection.
 *
 * No DB / no HTTP. Just exercises the static heuristic so a regression
 * in keyword matching is caught fast.
 */
class DrillAssetSelectorTest extends TestCase
{
    #[DataProvider('csvCases')]
    public function test_csv_filename_dispatch(string $name, ?string $expectedAsset, string $expectedRoute): void
    {
        $result = DrillAssetSelector::select('csv', $name);

        $this->assertSame($expectedAsset, $result['asset_key']);
        $this->assertSame($expectedRoute, $result['route']);
    }

    public static function csvCases(): array
    {
        return [
            'collar plain' => ['collar.csv', 'silver_collars', 'dagster'],
            'collar plural' => ['collars_2024.csv', 'silver_collars', 'dagster'],
            'drillhole alias' => ['drillholes_q1.csv', 'silver_collars', 'dagster'],
            'lithology' => ['lithology_log.csv', 'silver_lithology', 'dagster'],
            'geology alias' => ['geology_codes.csv', 'silver_lithology', 'dagster'],
            'survey' => ['surveys.csv', 'silver_surveys', 'dagster'],
            'deviation alias' => ['deviation_shots.csv', 'silver_surveys', 'dagster'],
            'sample' => ['samples_2024.csv', 'silver_samples', 'dagster'],
            'assay alias' => ['assay_results.csv', 'silver_samples', 'dagster'],
            'geochem alias' => ['geochem_data.csv', 'silver_samples', 'dagster'],
            'no hint' => ['data.csv', null, 'unrouted'],
        ];
    }

    public function test_xlsx_routes_to_silver_xlsx_for_internal_sheet_dispatch(): void
    {
        $result = DrillAssetSelector::select('xlsx', 'any_workbook.xlsx');

        $this->assertSame('silver_xlsx', $result['asset_key']);
        $this->assertSame('dagster', $result['route']);
    }

    public function test_xls_legacy_excel_routes_same_as_xlsx(): void
    {
        $result = DrillAssetSelector::select('xls', 'old_workbook.xls');

        $this->assertSame('silver_xlsx', $result['asset_key']);
        $this->assertSame('dagster', $result['route']);
    }

    public function test_pdf_routes_to_fastapi_bridge(): void
    {
        $result = DrillAssetSelector::select('pdf', 'NI43-101_2024.pdf');

        $this->assertNull($result['asset_key']);
        $this->assertSame('fastapi_pdf', $result['route']);
    }

    public function test_unknown_extension_is_unrouted(): void
    {
        $result = DrillAssetSelector::select('las', 'gamma_log.las');

        $this->assertNull($result['asset_key']);
        $this->assertSame('unrouted', $result['route']);
    }

    public function test_case_insensitive_matching(): void
    {
        $result = DrillAssetSelector::select('CSV', 'COLLARS_2024.CSV');

        $this->assertSame('silver_collars', $result['asset_key']);
        $this->assertSame('dagster', $result['route']);
    }
}
