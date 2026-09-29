<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Regression for database audit 2026-09-29 PG-2.
 *
 * Thirteen FKs to silver.collars (the typed drill tables and three gold
 * drillhole tables) were NO ACTION, so deleting a project — which cascades
 * into silver.collars — or a single collar failed with
 * `violates foreign key constraint "alteration_collar_id_fkey"` as soon as
 * any typed row existed. 2026_09_29_210200 makes them ON DELETE CASCADE.
 *
 * One row goes into every one of the thirteen tables, so a table that is
 * ever re-created without the cascade fails here by name.
 */
final class ProjectControllerDeleteTypedDrillDataTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    /** @var list<string> */
    private const TYPED_TABLES = [
        'silver.assays_v2',
        'silver.lithology',
        'silver.structure',
        'silver.alteration',
        'silver.mineralization',
        'silver.recovery',
        'silver.specific_gravity',
        'silver.geotechnical',
        'silver.downhole_geophysics',
        'silver.sample_intervals',
        'gold.assay_composites',
        'gold.significant_intersections',
        'gold.drill_summaries',
    ];

    public function test_every_collar_fk_cascades(): void
    {
        $rows = DB::select(
            "SELECT conrelid::regclass::text AS tbl, conname
               FROM pg_constraint
              WHERE contype = 'f'
                AND confrelid = 'silver.collars'::regclass
                AND confdeltype <> 'c'",
        );

        $this->assertSame([], array_map(fn (object $r): string => "{$r->tbl}.{$r->conname}", $rows));
    }

    public function test_destroying_a_project_removes_its_typed_drill_rows(): void
    {
        [$user, $project, $workspaceId] = $this->projectWithOwner();
        $collarId = $this->collarWithTypedRows($project->project_id, $workspaceId);

        $this->actingAs($user)
            ->deleteJson("/api/v1/projects/{$project->project_id}")
            ->assertNoContent();

        $this->assertDatabaseMissing('silver.projects', ['project_id' => $project->project_id]);
        $this->assertTypedRowsGone($collarId);
    }

    public function test_destroying_a_collar_removes_its_typed_drill_rows(): void
    {
        [$user, $project, $workspaceId] = $this->projectWithOwner();
        $collarId = $this->collarWithTypedRows($project->project_id, $workspaceId);

        $this->actingAs($user)
            ->deleteJson("/api/v1/projects/{$project->project_id}/collars/{$collarId}")
            ->assertNoContent();

        $this->assertDatabaseMissing('silver.collars', ['collar_id' => $collarId]);
        $this->assertTypedRowsGone($collarId);
    }

    /**
     * @return array{0: User, 1: Project, 2: string}
     */
    private function projectWithOwner(): array
    {
        $user = User::factory()->create();
        $workspaceId = (string) Str::uuid();

        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())',
            [$workspaceId, 'Typed Drill Delete Workspace', 'typed-del-'.substr($workspaceId, 0, 8)],
        );

        $project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$workspaceId, $project->project_id],
        );
        $user->projects()->syncWithoutDetaching([$project->project_id => ['role' => 'owner']]);

        return [$user, $project, $workspaceId];
    }

    private function collarWithTypedRows(string $projectId, string $workspaceId): string
    {
        $collarId = (string) Str::uuid();
        DB::table('silver.collars')->insert([
            'collar_id' => $collarId,
            'hole_id' => 'DH-CASCADE-1',
            'project_id' => $projectId,
            'workspace_id' => $workspaceId,
            'easting' => 500000.0,
            'northing' => 6000000.0,
            'total_depth' => 250.0,
            'hole_type' => 'DD',
            'status' => 'complete',
        ]);

        $base = ['workspace_id' => $workspaceId, 'collar_id' => $collarId];
        $interval = $base + ['from_depth' => 10, 'to_depth' => 12];

        DB::table('silver.assays_v2')->insert($interval + ['sample_id' => 'S-1', 'element' => 'Au', 'unit' => 'g/t']);
        DB::table('silver.lithology')->insert($interval);
        DB::table('silver.structure')->insert($base + ['depth' => 11, 'structure_type' => 'fault']);
        DB::table('silver.alteration')->insert($interval + ['alteration_type' => 'sericite']);
        DB::table('silver.mineralization')->insert($interval + ['mineral' => 'pyrite']);
        DB::table('silver.recovery')->insert($interval);
        DB::table('silver.specific_gravity')->insert($interval + ['sg_value' => 2.7]);
        DB::table('silver.geotechnical')->insert($interval);
        DB::table('silver.downhole_geophysics')->insert($base + ['depth' => 11, 'reading_type' => 'gamma', 'value' => 42, 'unit' => 'cps']);
        DB::table('silver.sample_intervals')->insert($interval + ['sample_id' => 'S-1', 'sample_type' => 'core']);
        DB::table('gold.assay_composites')->insert($interval + ['composite_type' => 'fixed_length', 'element' => 'Au', 'weighted_avg' => 1.2, 'unit' => 'g/t']);
        DB::table('gold.significant_intersections')->insert($interval + ['element' => 'Au', 'cutoff_grade' => 0.5, 'weighted_avg' => 1.2, 'unit' => 'g/t']);
        DB::table('gold.drill_summaries')->insert($base + ['hole_id' => 'DH-CASCADE-1']);

        foreach (self::TYPED_TABLES as $table) {
            $this->assertSame(1, DB::table($table)->where('collar_id', $collarId)->count(), "{$table} fixture row");
        }

        return $collarId;
    }

    private function assertTypedRowsGone(string $collarId): void
    {
        foreach (self::TYPED_TABLES as $table) {
            $this->assertSame(0, DB::table($table)->where('collar_id', $collarId)->count(), "{$table} should cascade");
        }
    }
}
