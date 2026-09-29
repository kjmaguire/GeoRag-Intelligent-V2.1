<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\Project;
use App\Models\User;
use App\Support\SetsWorkspaceRlsContext;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Inertia\Testing\AssertableInertia;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * LAR-4 (2026-09-29 audit): one failed optional panel query must not poison
 * the rest of the request.
 *
 * withWorkspaceRls() runs the whole action in ONE transaction so the
 * `app.workspace_id` GUC holds for every statement. On Postgres the first
 * failed statement aborts that transaction (25P02), so every later statement
 * failed too: WorkspaceController's later panels silently rendered empty, and
 * DrillholeDetailController's unguarded data-quality summary 500'd the page.
 * SQLite does not abort a transaction on error, which is why only this suite
 * can see it.
 *
 * The breakage used here is the realistic one: the image is live against a
 * schema the `migrate` task has not caught up with, so a column a reader
 * expects is not there. Renaming a column inside the test's own transaction
 * reproduces that and is rolled back with everything else.
 */
final class OptionalPanelSavepointTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private string $workspaceId;

    /**
     * @return array{user: User, project: Project}
     */
    private function seedProject(): array
    {
        $user = User::factory()->create();

        $this->workspaceId = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$this->workspaceId, 'Savepoint Workspace', 'sp-'.substr($this->workspaceId, 0, 8)],
        );

        $project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $project->project_id],
        );
        $user->projects()->syncWithoutDetaching([$project->project_id => ['role' => 'viewer']]);

        return ['user' => $user, 'project' => $project];
    }

    private function seedCollar(Project $project): string
    {
        $collarId = (string) Str::uuid();
        DB::statement(
            "INSERT INTO silver.collars (
                collar_id, hole_id, project_id, workspace_id,
                easting, northing, elevation, total_depth, azimuth, dip,
                hole_type, status, geom
             ) VALUES (
                ?::uuid, 'SP-001', ?::uuid, ?::uuid,
                500000, 4500000, 1000, 150, 180, -60,
                'DDH', 'completed',
                ST_SetSRID(ST_MakePoint(500000, 4500000), 32613)
             )",
            [$collarId, $project->project_id, $this->workspaceId],
        );

        return $collarId;
    }

    /**
     * Every gold interval reader selects lithology_code, so renaming it
     * breaks the strip tracks, the ore aggregates and the 3D bands while
     * leaving the other tables alone.
     */
    private function breakGoldIntervalReaders(): void
    {
        DB::statement('ALTER TABLE gold.drillhole_intervals_visual RENAME COLUMN lithology_code TO lithology_code_pending_migration');
    }

    public function test_drillhole_detail_survives_a_failed_panel_and_still_reads_later_panels(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject();
        $collarId = $this->seedCollar($project);

        DB::table('silver.data_quality_flags')->insert([
            'workspace_id' => $this->workspaceId,
            'project_id' => $project->project_id,
            'record_type' => 'collar',
            'record_id' => $collarId,
            'flag_type' => 'collar.missing_elevation',
            'severity' => 'WARNING',
            'description' => 'savepoint fixture',
        ]);

        $this->breakGoldIntervalReaders();

        $this->actingAs($user)
            ->get('/projects/'.$project->slug.'/holes/'.$collarId.'/detail')
            ->assertOk()
            ->assertInertia(fn (AssertableInertia $page) => $page
                ->component('Foundry/DrillholeDetail')
                // The broken panel degrades to its empty shape...
                ->where('strip_tracks.lithology', [])
                // ...and the panels read after it still see real rows.
                ->where('data_quality_flags.open_total', 1)
                ->where('data_quality_flags.counts.WARNING', 1)
                ->where('data_quality_flags.evaluated', true),
            );
    }

    public function test_workspace_panels_after_a_failed_panel_still_populate(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject();
        $collarId = $this->seedCollar($project);

        DB::table('silver.lithology_logs')->insert([
            'log_id' => (string) Str::uuid(),
            'collar_id' => $collarId,
            'workspace_id' => $this->workspaceId,
            'from_depth' => 0,
            'to_depth' => 10,
            'lithology_code' => 'SST',
        ]);

        $this->breakGoldIntervalReaders();

        $this->actingAs($user)
            ->get('/projects/'.$project->slug.'/workspace')
            ->assertOk()
            ->assertInertia(fn (AssertableInertia $page) => $page
                ->component('Foundry/Workspace')
                ->where('project_summary.ore_hole_count', 0)
                // silver.lithology_logs is counted long after the first
                // gold interval read fails. Before the savepoints it came
                // back 0 from an aborted transaction.
                ->where('project_layers', fn ($layers): bool => collect($layers)
                    ->firstWhere('id', 'lithology')['count'] === 1)
                ->where('project_summary.total_drilled_m', 150),
            );
    }

    /**
     * The deferred `viz3d` group (FE-11) is built in its own RLS transaction
     * after the page's transaction commits, so it needs its own savepoints:
     * the interval bands are read first, and before LAR-4 reached
     * buildThreeDPayload() their failure emptied every 3D panel after them.
     */
    public function test_deferred_3d_group_survives_a_failed_block_and_still_reads_later_blocks(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject();
        $collarId = $this->seedCollar($project);

        DB::table('silver.surveys')->insert([
            'survey_id' => (string) Str::uuid(),
            'collar_id' => $collarId,
            'workspace_id' => $this->workspaceId,
            'depth' => 50,
            'azimuth' => 180,
            'dip' => -60,
            'survey_method' => 'gyro',
        ]);

        $this->breakGoldIntervalReaders();

        $this->actingAs($user)
            ->get('/projects/'.$project->slug.'/workspace')
            ->assertOk()
            ->assertInertia(fn (AssertableInertia $page) => $page
                ->component('Foundry/Workspace')
                ->loadDeferredProps('viz3d', fn (AssertableInertia $reload) => $reload
                    // The broken block degrades to its empty shape...
                    ->where('first_holes_intervals', [])
                    // ...and the survey block read after it still sees its row.
                    ->has('surveys_3d', 1)
                    ->where('surveys_3d.0.collar_id', $collarId)),
            );
    }

    public function test_optional_query_rolls_back_only_its_savepoint_and_keeps_the_rls_guc(): void
    {
        $workspaceId = (string) Str::uuid();

        $probe = new class
        {
            use SetsWorkspaceRlsContext;

            /**
             * @return array{failed: mixed, after: mixed, guc: mixed}
             */
            public function run(string $workspaceId): array
            {
                return $this->withWorkspaceRls($workspaceId, function (): array {
                    $failed = $this->optionalQuery(
                        static fn () => DB::selectOne('SELECT no_such_column FROM silver.collars LIMIT 1'),
                        'fallback',
                    );

                    return [
                        'failed' => $failed,
                        'after' => DB::selectOne('SELECT 41 + 1 AS answer')->answer,
                        'guc' => DB::selectOne("SELECT current_setting('app.workspace_id', true) AS ws")->ws,
                    ];
                });
            }
        };

        $levelBefore = DB::transactionLevel();
        $result = $probe->run($workspaceId);

        $this->assertSame('fallback', $result['failed']);
        $this->assertSame(42, (int) $result['after']);
        $this->assertSame($workspaceId, $result['guc']);
        $this->assertSame($levelBefore, DB::transactionLevel(), 'savepoint levels must balance');
    }
}
