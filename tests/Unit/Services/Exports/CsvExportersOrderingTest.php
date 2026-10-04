<?php

declare(strict_types=1);

namespace Tests\Unit\Services\Exports;

use App\Services\Exports\CsvAssaysExporter;
use App\Services\Exports\CsvLithologyExporter;
use App\Services\Exports\CsvSamplesExporter;
use Illuminate\Support\Facades\DB;
use Tests\TestCase;

/**
 * The CSV exporters page with offset chunk(2000). Offset paging is only
 * correct over a total order, so each query must end its ORDER BY with the
 * table's primary key; otherwise rows sharing (hole_id, from_depth, ...) can
 * be duplicated or dropped across page boundaries.
 *
 * DB::pretend() captures the SQL without executing it, so no tables needed.
 */
final class CsvExportersOrderingTest extends TestCase
{
    /**
     * @return array<int, string> the ORDER BY clause of every statement run
     */
    private function orderByClauses(callable $export): array
    {
        $clauses = [];
        foreach (DB::pretend($export) as $query) {
            if (preg_match('/order by (.+?)(?: limit |$)/i', (string) $query['query'], $m)) {
                $clauses[] = str_replace('"', '', $m[1]);
            }
        }

        return $clauses;
    }

    public function test_lithology_export_orders_by_primary_key_last(): void
    {
        $clauses = $this->orderByClauses(function (): void {
            $r = (new CsvLithologyExporter)->export('00000000-0000-0000-0000-000000000001');
            @unlink($r['path']);
        });

        $this->assertNotEmpty($clauses);
        $this->assertStringEndsWith('l.id asc', $clauses[0]);
    }

    public function test_samples_export_orders_by_primary_key_last(): void
    {
        $clauses = $this->orderByClauses(function (): void {
            $r = (new CsvSamplesExporter)->export('00000000-0000-0000-0000-000000000001');
            @unlink($r['path']);
        });

        $this->assertNotEmpty($clauses);
        $this->assertStringEndsWith('s.sample_id asc', $clauses[0]);
    }

    public function test_assays_export_orders_by_primary_key_last(): void
    {
        $clauses = $this->orderByClauses(function (): void {
            $r = (new CsvAssaysExporter)->export('00000000-0000-0000-0000-000000000001');
            @unlink($r['path']);
        });

        $this->assertNotEmpty($clauses);
        $this->assertStringEndsWith('a.element asc, a.id asc', $clauses[0]);
    }
}
