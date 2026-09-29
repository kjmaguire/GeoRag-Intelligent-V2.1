<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\Project;
use App\Models\User;
use App\Support\HoleId;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Inertia\Testing\AssertableInertia;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * The workspace drillhole readers used to hide valid data.
 *
 *   - LOGS listed only holes with a curve named exactly GAMMA and plotted only
 *     GAMMA / GRADE / RES / SP. A hole logged with GR, RESIST, IP, SUSC, DEN
 *     or CAL rendered nothing.
 *   - The map capped at 500 collars, 3D intervals at 200, and surveys at
 *     20,000 rows in collar_id order, so late holes silently lost their
 *     trajectories.
 *
 * Postgres-only: window functions over PostGIS-backed fixtures and
 * `double precision[]` curve columns (phpunit.pgsql.xml).
 */
final class WorkspaceLogsAndLimitsTest extends TestCase
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
            [$this->workspaceId, 'Logs And Limits Workspace', 'wll-'.substr($this->workspaceId, 0, 8)],
        );

        $project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $project->project_id],
        );
        $user->projects()->syncWithoutDetaching([$project->project_id => ['role' => 'viewer']]);

        return ['user' => $user, 'project' => $project];
    }

    private function seedCollar(Project $project, string $holeId): string
    {
        $collarId = (string) Str::uuid();
        DB::statement(
            "INSERT INTO silver.collars (
                collar_id, hole_id, project_id, workspace_id,
                easting, northing, elevation, total_depth, azimuth, dip,
                hole_type, status, geom_4326
             ) VALUES (
                ?::uuid, ?, ?::uuid, ?::uuid,
                500000, 4500000, 1000, 150, 180, -60,
                'DDH', 'completed',
                ST_Transform(ST_SetSRID(ST_MakePoint(500000, 4500000), 32613), 4326)
             )",
            [$collarId, $holeId, $project->project_id, $this->workspaceId],
        );

        return $collarId;
    }

    /**
     * @param list<float> $values one value per metre, from 0 m
     */
    private function seedCurve(string $collarId, string $name, array $values, ?string $unit = null): void
    {
        $depths = array_keys($values);

        DB::statement(
            'INSERT INTO silver.well_log_curves (
                curve_id, collar_id, workspace_id, curve_name, curve_unit,
                min_depth, max_depth, step, null_value, sample_count,
                depths, values, created_at, updated_at
             ) VALUES (
                ?::uuid, ?::uuid, ?::uuid, ?, ?,
                0, ?, 1, -999.25, ?,
                ?::double precision[], ?::double precision[], NOW(), NOW()
             )',
            [
                (string) Str::uuid(), $collarId, $this->workspaceId, $name, $unit,
                (float) max($depths), count($values),
                '{'.implode(',', $depths).'}',
                '{'.implode(',', $values).'}',
            ],
        );
    }

    private function seedSurveyStations(string $collarId, int $count): void
    {
        DB::statement(
            "INSERT INTO silver.surveys (survey_id, collar_id, workspace_id, depth, azimuth, dip, survey_method, created_at, updated_at)
             SELECT gen_random_uuid(), ?::uuid, ?::uuid, g::float8, 180, -60, 'test', NOW(), NOW()
               FROM generate_series(0, ?) AS g",
            [$collarId, $this->workspaceId, $count - 1],
        );
    }

    /**
     * @return array<string, mixed>
     */
    private function workspaceProps(User $user, Project $project, string $query = ''): array
    {
        $props = [];
        $this->actingAs($user)
            ->get('/projects/'.$project->slug.'/workspace'.$query)
            ->assertStatus(200)
            ->assertInertia(function (AssertableInertia $page) use (&$props) {
                $props = $page->toArray()['props'];
                // The 3D group is deferred (FE-11); fetch it the way the
                // client does and merge, so assertions see the whole page.
                $page->loadDeferredProps('viz3d', function (AssertableInertia $reload) use (&$props) {
                    $props = array_merge($props, $reload->toArray()['props']);
                });

                return $page;
            });

        return $props;
    }

    public function test_hole_without_a_gamma_curve_is_listed_and_plotted(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject();
        $withGr = $this->seedCollar($project, 'RS-001');
        $this->seedCollar($project, 'RS-002'); // no curves at all
        $this->seedCurve($withGr, 'RESIST', [10.0, 12.0, 14.0, 13.0], 'ohm.m');
        $this->seedCurve($withGr, 'GR', [50.0, 60.0, 55.0, 70.0], 'API');

        $props = $this->workspaceProps($user, $project);

        // The panel keys and labels holes by hole_id_canonical, which the
        // database now always derives (trg_collars_hole_id_canonical, §04e
        // 2026-09-29) — it used to be NULL for a collar written without one.
        $this->assertSame(['RS001'], $props['log_hole_options'], 'only holes with a curve are listed, whatever its name');
        $this->assertSame('RS001', $props['log_hole_id']);
        $this->assertCount(2, $props['log_tracks']);
    }

    public function test_hole_with_several_curves_is_listed_once(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject();
        $collarId = $this->seedCollar($project, 'RS-001');
        foreach (['GR', 'IP', 'SUSC', 'DEN', 'CAL'] as $name) {
            $this->seedCurve($collarId, $name, [1.0, 2.0, 3.0]);
        }

        $props = $this->workspaceProps($user, $project);

        $this->assertSame(['RS001'], $props['log_hole_options']);
    }

    public function test_available_curves_list_every_curve_with_units_and_gamma_family_first(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject();
        $collarId = $this->seedCollar($project, 'RS-001');
        $this->seedCurve($collarId, 'CAL', [6.0, 6.1, 6.2], 'in');
        $this->seedCurve($collarId, 'ZZ_CUSTOM', [1.0, 2.0, 3.0]); // unknown name: must survive
        $this->seedCurve($collarId, 'RT', [5.0, 6.0, 7.0], 'ohm.m');
        $this->seedCurve($collarId, 'GR', [50.0, 60.0, 70.0], 'API');

        $props = $this->workspaceProps($user, $project);

        $names = array_column($props['log_available_curves'], 'curve_name');
        $this->assertSame(['GR', 'RT', 'CAL', 'ZZ_CUSTOM'], $names);

        $byName = array_column($props['log_available_curves'], null, 'curve_name');
        $this->assertSame('API', $byName['GR']['unit']);
        $this->assertSame('gamma', $byName['GR']['group']);
        $this->assertSame('resistivity', $byName['RT']['group']);
        $this->assertSame('other', $byName['ZZ_CUSTOM']['group']);
        $this->assertNull($byName['ZZ_CUSTOM']['unit']);

        // Nothing is dropped from the plot either (4 curves < default cap).
        $this->assertSame($names, array_column($props['log_tracks'], 'curve'));
        $this->assertSame($names, $props['log_selected_curves']);
    }

    public function test_default_selection_is_bounded_but_the_full_list_is_still_returned(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject();
        $collarId = $this->seedCollar($project, 'RS-001');
        for ($i = 1; $i <= 15; $i++) {
            $this->seedCurve($collarId, sprintf('C%02d', $i), [1.0, 2.0, 3.0]);
        }

        $props = $this->workspaceProps($user, $project);

        $this->assertCount(15, $props['log_available_curves']);
        $this->assertCount(8, $props['log_tracks']);
        $this->assertCount(8, $props['log_selected_curves']);
        $this->assertSame(12, $props['log_curves_max']);
    }

    public function test_log_curves_query_selects_named_curves_and_ignores_unknown_names(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject();
        $collarId = $this->seedCollar($project, 'RS-001');
        $this->seedCurve($collarId, 'GR', [50.0, 60.0, 70.0]);
        $this->seedCurve($collarId, 'CAL', [6.0, 6.1, 6.2]);
        $this->seedCurve($collarId, 'DEN', [2.1, 2.2, 2.3]);

        $props = $this->workspaceProps($user, $project, '?log_curves=DEN,CAL,NOT_A_CURVE');

        $this->assertSame(['DEN', 'CAL'], $props['log_selected_curves']);
        $this->assertSame(['DEN', 'CAL'], array_column($props['log_tracks'], 'curve'));
        $this->assertCount(3, $props['log_available_curves']);

        $props = $this->workspaceProps($user, $project, '?log_curves=NOT_A_CURVE');
        $this->assertCount(3, $props['log_tracks'], 'an all-unknown selection falls back to the default');
    }

    public function test_null_sentinel_samples_are_not_plotted(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject();
        $collarId = $this->seedCollar($project, 'RS-001');
        $this->seedCurve($collarId, 'GR', [50.0, -999.25, 70.0]);

        $props = $this->workspaceProps($user, $project);

        $this->assertCount(2, $props['log_tracks'][0]['points']);
    }

    public function test_hole_payload_returns_every_curve_not_just_the_legacy_four(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject();
        $collarId = $this->seedCollar($project, 'RS-001');
        $this->seedCurve($collarId, 'SUSC', [0.1, 0.2, 0.3], 'SI');
        $this->seedCurve($collarId, 'GR', [50.0, 60.0, 70.0], 'API');

        $response = $this->actingAs($user)->get('/projects/'.$project->slug.'/holes/RS-001/payload');

        $response->assertStatus(200);
        $response->assertJsonCount(2, 'log_tracks');
        $response->assertJsonCount(2, 'log_available_curves');
        $this->assertSame(['GR', 'SUSC'], array_column($response->json('log_tracks'), 'curve'));
    }

    public function test_surveys_are_returned_for_every_returned_collar(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject();
        $ids = [];
        foreach (['RS-001', 'RS-002', 'RS-003'] as $hole) {
            $ids[$hole] = $this->seedCollar($project, $hole);
        }
        $this->seedSurveyStations($ids['RS-001'], 10);
        $this->seedSurveyStations($ids['RS-002'], 250); // over the per-hole bound
        $this->seedSurveyStations($ids['RS-003'], 10);

        $props = $this->workspaceProps($user, $project);

        $byCollar = [];
        foreach ($props['surveys_3d'] as $s) {
            $byCollar[$s['collar_id']][] = $s['depth'];
        }

        $this->assertEqualsCanonicalizing(array_values($ids), array_keys($byCollar), 'no returned hole loses its survey');
        $this->assertCount(10, $byCollar[$ids['RS-001']]);
        $this->assertCount(10, $byCollar[$ids['RS-003']]);
        $this->assertLessThanOrEqual(100, count($byCollar[$ids['RS-002']]));
        $this->assertGreaterThan(50, count($byCollar[$ids['RS-002']]));
        $this->assertEquals(0, min($byCollar[$ids['RS-002']]), 'first station is kept');
        $this->assertEquals(249, max($byCollar[$ids['RS-002']]), 'last station is kept');
        $this->assertSame(1, $props['survey_holes_downsampled']);
    }

    public function test_surveys_are_not_returned_for_another_projects_collars(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject();
        $own = $this->seedCollar($project, 'RS-001');
        $this->seedSurveyStations($own, 3);

        // A second project (and workspace) with surveys of its own.
        ['project' => $other] = $this->seedProject();
        $foreign = $this->seedCollar($other, 'OTHER-001');
        $this->seedSurveyStations($foreign, 5);

        $props = $this->workspaceProps($user, $project);

        $this->assertSame([$own], array_values(array_unique(array_column($props['surveys_3d'], 'collar_id'))));
        $this->assertNotContains($foreign, array_column($props['collars'], 'collar_id'));
    }

    public function test_small_project_reports_no_truncation(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject();
        $this->seedCollar($project, 'RS-001');
        $this->seedCollar($project, 'RS-002');

        $props = $this->workspaceProps($user, $project);

        $this->assertSame(
            [
                'collars' => ['shown' => 2, 'total' => 2, 'truncated' => false],
                'interval_holes' => ['shown' => 2, 'total' => 2, 'truncated' => false],
            ],
            $props['truncation'],
        );
        $this->assertSame(0, $props['survey_holes_downsampled']);
    }

    public function test_collar_cap_sets_truncated_flag_and_keeps_hole_id_order(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject();

        // 1,001 collars in one statement: one over MAX_WORKSPACE_COLLARS.
        DB::statement(
            "INSERT INTO silver.collars (
                collar_id, hole_id, project_id, workspace_id,
                easting, northing, elevation, total_depth, azimuth, dip,
                hole_type, status, geom_4326
             )
             SELECT gen_random_uuid(), 'CAP-'||lpad(g::text, 4, '0'), ?::uuid, ?::uuid,
                    500000, 4500000, 1000, 150, 180, -60,
                    'DDH', 'completed',
                    ST_Transform(ST_SetSRID(ST_MakePoint(500000, 4500000), 32613), 4326)
               FROM generate_series(1, 1001) AS g",
            [$project->project_id, $this->workspaceId],
        );

        $props = $this->workspaceProps($user, $project);

        $this->assertCount(1000, $props['collars']);
        $holes = array_column($props['collars'], 'hole_id');
        $sorted = $holes;
        sort($sorted, SORT_STRING);
        $this->assertSame($sorted, $holes, 'collars come back in hole_id order');
        $this->assertSame('CAP-0001', $holes[0]);
        $this->assertSame('CAP-1000', $holes[999], 'the cap drops the LAST hole in hole_id order, deterministically');

        $this->assertSame(['shown' => 1000, 'total' => 1001, 'truncated' => true], $props['truncation']['collars']);
        $this->assertSame(['shown' => 200, 'total' => 1001, 'truncated' => true], $props['truncation']['interval_holes']);
        $this->assertCount(200, $props['first_holes_intervals']);
        // first_holes_intervals names holes by their canonical id (which
        // the database always derives since §04e 2026-09-29); `collars`
        // carries the stored spelling.
        $this->assertSame(
            array_map(HoleId::canonicalize(...), array_slice($holes, 0, 200)),
            array_column($props['first_holes_intervals'], 'hole_id'),
            'the 3D interval holes are a prefix of the returned collars',
        );
    }
}
