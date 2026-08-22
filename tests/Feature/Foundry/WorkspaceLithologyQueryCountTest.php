<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Illuminate\Testing\TestResponse;
use Inertia\Testing\AssertableInertia;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * The lithology payload used to cost one query per hole.
 *
 * `WorkspaceController::show()` looped over up to 200 collars and ran a
 * separate `SELECT ... LIMIT 80` for each one's bands. Two things made
 * that more than a slow page:
 *
 *   - `withWorkspaceRls()` wraps the whole action in `DB::transaction()`,
 *     and PgBouncer runs in transaction pooling, so ONE server connection
 *     stayed pinned across all 200 sequential round trips. A handful of
 *     concurrent workspace loads exhausted the server-side pool and every
 *     other query in the application queued behind them.
 *   - The cost scaled with the project. A two-hole demo project looked
 *     fine; a real Athabasca project with 200 holes did not.
 *
 * A single `ROW_NUMBER() OVER (PARTITION BY collar_id ORDER BY depth_from)`
 * replaces the loop and reproduces the per-hole cap exactly. A plain
 * `whereIn` with one global LIMIT would not: one deep hole would consume
 * the whole budget and every hole after it would come back empty.
 *
 * Postgres-only — window functions plus the PostGIS columns the fixture
 * writes.
 */
final class WorkspaceLithologyQueryCountTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    /**
     * @return array{user: User, project: Project, holes: list<string>}
     */
    private function seedProject(int $collarCount, int $bandsPerHole): array
    {
        $user = User::factory()->create();
        $workspaceId = (string) Str::uuid();

        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$workspaceId, 'Lithology N+1 Workspace', 'wln-'.substr($workspaceId, 0, 8)],
        );

        $project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$workspaceId, $project->project_id],
        );
        $user->projects()->syncWithoutDetaching([$project->project_id => ['role' => 'viewer']]);

        $holes = [];
        for ($i = 1; $i <= $collarCount; $i++) {
            $collarId = (string) Str::uuid();
            $holeId = sprintf('WLN-%03d', $i);
            $holes[] = $holeId;

            DB::statement(
                "INSERT INTO silver.collars (
                    collar_id, hole_id, project_id, workspace_id,
                    easting, northing, elevation, total_depth, azimuth, dip,
                    hole_type, status, geom
                 ) VALUES (
                    ?::uuid, ?, ?::uuid, ?::uuid,
                    500000, 4500000, 1000, 500, 180, -60,
                    'DDH', 'completed',
                    ST_SetSRID(ST_MakePoint(500000, 4500000), 32613)
                 )",
                [$collarId, $holeId, $project->project_id, $workspaceId],
            );

            for ($b = 0; $b < $bandsPerHole; $b++) {
                DB::statement(
                    "INSERT INTO gold.drillhole_intervals_visual (
                        collar_id, workspace_id, project_id,
                        depth_from, depth_to, interval_kind,
                        lithology_code, color_hint
                     ) VALUES (?::uuid, ?::uuid, ?::uuid, ?, ?, 'lithology', ?, '#334455')",
                    [
                        $collarId, $workspaceId, $project->project_id,
                        $b * 2.0, ($b * 2.0) + 2.0,
                        // Encodes the hole and the ordinal, so a test can see
                        // both mis-grouping and mis-ordering.
                        $holeId.'-B'.$b,
                    ],
                );
            }
        }

        return ['user' => $user, 'project' => $project, 'holes' => $holes];
    }

    /** @return array{queries: int, page: TestResponse} */
    private function loadWorkspace(User $user, Project $project): array
    {
        $intervalQueries = 0;
        DB::listen(function ($query) use (&$intervalQueries) {
            if (str_contains($query->sql, 'drillhole_intervals_visual')) {
                $intervalQueries++;
            }
        });

        $page = $this->actingAs($user)->get('/projects/'.$project->slug.'/workspace');

        return ['queries' => $intervalQueries, 'page' => $page];
    }

    public function test_the_query_count_does_not_grow_with_the_number_of_holes(): void
    {
        $small = $this->seedProject(collarCount: 2, bandsPerHole: 2);
        $smallResult = $this->loadWorkspace($small['user'], $small['project']);
        $smallResult['page']->assertStatus(200);

        $large = $this->seedProject(collarCount: 12, bandsPerHole: 2);
        $largeResult = $this->loadWorkspace($large['user'], $large['project']);
        $largeResult['page']->assertStatus(200);

        // The controller runs several other aggregates against this table
        // (ore-band counts, intervals_count, the 3D sub-views), so the
        // absolute number is not the contract. That it is FLAT is.
        $this->assertSame(
            $smallResult['queries'],
            $largeResult['queries'],
            'Six times the holes must not mean six times the queries — the '
            .'per-collar loop is what pinned a pooled connection.',
        );
    }

    public function test_each_hole_still_gets_its_own_bands_in_depth_order(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject(
            collarCount: 3, bandsPerHole: 4,
        );

        $response = $this->actingAs($user)->get('/projects/'.$project->slug.'/workspace');

        $response->assertInertia(
            fn (AssertableInertia $page) => $page->where(
                'first_holes_intervals',
                function ($holes) {
                    $holes = json_decode(json_encode($holes), true);
                    if (count($holes) !== 3) {
                        return false;
                    }
                    foreach ($holes as $hole) {
                        if (count($hole['bands']) !== 4) {
                            return false;
                        }
                        foreach ($hole['bands'] as $ordinal => $band) {
                            // Grouping: every band belongs to its own hole.
                            if ($band['code'] !== $hole['hole_id'].'-B'.$ordinal) {
                                return false;
                            }
                            // Ordering: ROW_NUMBER's ORDER BY depth_from.
                            if ((float) $band['from'] !== $ordinal * 2.0) {
                                return false;
                            }
                        }
                    }

                    return true;
                },
            ),
        );
    }

    public function test_a_hole_with_no_bands_still_appears(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject(
            collarCount: 2, bandsPerHole: 0,
        );

        $response = $this->actingAs($user)->get('/projects/'.$project->slug.'/workspace');

        $response->assertInertia(
            fn (AssertableInertia $page) => $page->where(
                'first_holes_intervals',
                function ($holes) {
                    $holes = json_decode(json_encode($holes), true);

                    return count($holes) === 2
                        && $holes[0]['bands'] === []
                        && $holes[1]['bands'] === [];
                },
            ),
        );
    }
}
