<?php

declare(strict_types=1);

namespace Tests\Feature\Database;

use App\Models\Project;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Regression for database audit 2026-09-29 PG-1.
 *
 * silver.mv_collar_summary joined samples AND lithology_logs straight onto
 * collars, so each collar contributed n_samples x n_litho rows before the
 * GROUP BY. With two holes (100 m, 300 m), 10 samples and 5 litho
 * intervals on the first, it reported total_collars=51, avg_depth=103.9,
 * total_samples=50. The orchestrator hands these to the LLM as
 * "HIGH-CONFIDENCE SUMMARIES (quote verbatim)".
 *
 * 2026_09_29_210300 pre-aggregates per collar. This pins the exact numbers,
 * the column types readers depend on, and the unique index that
 * REFRESH ... CONCURRENTLY needs.
 */
final class MvCollarSummaryAggregatesTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    public function test_counts_and_depths_are_not_multiplied_by_child_rows(): void
    {
        $workspaceId = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())',
            [$workspaceId, 'MV Summary Workspace', 'mv-sum-'.substr($workspaceId, 0, 8)],
        );
        $project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$workspaceId, $project->project_id],
        );

        $holeOne = $this->collar($project->project_id, $workspaceId, 'H1', 100.0, 'DD');
        $this->collar($project->project_id, $workspaceId, 'H2', 300.0, 'RC');
        // A hole with no downhole data at all must still count once.
        $holeThree = $this->collar($project->project_id, $workspaceId, 'H3', 200.0, 'DD');

        for ($i = 0; $i < 10; $i++) {
            DB::table('silver.samples')->insert([
                'sample_id' => (string) Str::uuid(),
                'collar_id' => $holeOne,
                'workspace_id' => $workspaceId,
                'from_depth' => $i,
                'to_depth' => $i + 1,
                'sample_type' => 'core',
            ]);
        }
        for ($i = 0; $i < 5; $i++) {
            DB::table('silver.lithology_logs')->insert([
                'log_id' => (string) Str::uuid(),
                'collar_id' => $holeOne,
                'workspace_id' => $workspaceId,
                'from_depth' => $i * 10,
                'to_depth' => $i * 10 + 10,
            ]);
        }
        // Samples on a second hole too, so the sum crosses collars.
        for ($i = 0; $i < 3; $i++) {
            DB::table('silver.samples')->insert([
                'sample_id' => (string) Str::uuid(),
                'collar_id' => $holeThree,
                'workspace_id' => $workspaceId,
                'from_depth' => $i,
                'to_depth' => $i + 1,
                'sample_type' => 'core',
            ]);
        }

        DB::statement('REFRESH MATERIALIZED VIEW silver.mv_collar_summary');
        // The CONCURRENTLY path the refresh helper uses needs the unique index.
        DB::statement('REFRESH MATERIALIZED VIEW CONCURRENTLY silver.mv_collar_summary');

        $row = DB::selectOne(
            'SELECT total_collars, avg_depth::text AS avg_depth, min_depth::text AS min_depth,
                    max_depth::text AS max_depth, hole_type_count,
                    total_samples, total_litho_intervals
               FROM silver.mv_collar_summary
              WHERE project_id = ?::uuid',
            [$project->project_id],
        );

        $this->assertNotNull($row);
        $this->assertSame(3, (int) $row->total_collars);
        $this->assertSame('200.0', $row->avg_depth);
        $this->assertSame('100.0', $row->min_depth);
        $this->assertSame('300.0', $row->max_depth);
        $this->assertSame(2, (int) $row->hole_type_count);
        $this->assertSame(13, (int) $row->total_samples);
        $this->assertSame(5, (int) $row->total_litho_intervals);
    }

    public function test_column_shape_is_unchanged_for_readers(): void
    {
        $columns = DB::select(
            "SELECT a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type
               FROM pg_attribute a
              WHERE a.attrelid = 'silver.mv_collar_summary'::regclass
                AND a.attnum > 0 AND NOT a.attisdropped
              ORDER BY a.attnum",
        );

        $this->assertSame([
            'project_id:uuid',
            'total_collars:bigint',
            'avg_depth:numeric(10,1)',
            'min_depth:numeric(10,1)',
            'max_depth:numeric(10,1)',
            'hole_type_count:bigint',
            'earliest_drill:date',
            'latest_drill:date',
            'total_samples:bigint',
            'total_litho_intervals:bigint',
        ], array_map(fn (object $c): string => "{$c->name}:{$c->type}", $columns));
    }

    private function collar(string $projectId, string $workspaceId, string $holeId, float $depth, string $type): string
    {
        $collarId = (string) Str::uuid();
        DB::table('silver.collars')->insert([
            'collar_id' => $collarId,
            'hole_id' => $holeId,
            'project_id' => $projectId,
            'workspace_id' => $workspaceId,
            'easting' => 500000.0,
            'northing' => 6000000.0,
            'total_depth' => $depth,
            'hole_type' => $type,
            'status' => 'complete',
        ]);

        return $collarId;
    }
}
