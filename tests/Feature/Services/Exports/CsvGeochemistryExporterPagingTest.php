<?php

declare(strict_types=1);

namespace Tests\Feature\Services\Exports;

use App\Services\Exports\CsvGeochemistryExporter;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\Concerns\SeedsCollarExportData;
use Tests\TestCase;

/**
 * csv_geochem pages its query with offset chunk(2000), which only lines up over
 * a TOTAL order. It ordered by (hole_id, from_depth, sample_id), and sample_id
 * is nullable, so rows tied on all three could be repeated or dropped where one
 * page ends and the next begins. The primary key is the final tiebreaker.
 *
 * Postgres-only: silver.geochemistry has a PostGIS geometry column.
 */
final class CsvGeochemistryExporterPagingTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;
    use SeedsCollarExportData;

    public function test_rows_tied_on_every_sort_key_each_appear_exactly_once_across_pages(): void
    {
        $project = $this->exportProject(26913);
        $collar = $this->exportCollar($project, 'GEO-1', ['x' => 500000.0, 'y' => 6000000.0, 'srid' => 26913]);

        // More than one 2,000-row page, all with the same collar, the same
        // depths and NO sample_id: every sort key but the primary key ties.
        $ids = [];
        $rows = [];
        for ($i = 0; $i < 2150; $i++) {
            $ids[] = $id = (string) Str::uuid();
            $rows[] = [
                'geochem_id' => $id,
                'collar_id' => $collar,
                'project_id' => $project->project_id,
                'workspace_id' => $project->workspace_id,
                'geom' => DB::raw('ST_SetSRID(ST_MakePoint(-105.0, 54.0), 4326)'),
                'from_depth' => 10.0,
                'to_depth' => 11.0,
                'sio2_wt_pct' => 60.0 + ($i % 7),
            ];
        }
        foreach (array_chunk($rows, 500) as $chunk) {
            DB::table('silver.geochemistry')->insert($chunk);
        }

        $exported = array_slice(
            $this->readCsv((new CsvGeochemistryExporter)->export($project->project_id)['path']),
            1,
        );
        $exportedIds = array_column($exported, 0);

        $this->assertCount(2150, $exportedIds);
        $this->assertCount(2150, array_unique($exportedIds), 'no row repeated across the page boundary');
        $this->assertEmpty(array_diff($ids, $exportedIds), 'no row dropped across the page boundary');
    }
}
