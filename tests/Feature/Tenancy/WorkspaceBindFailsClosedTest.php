<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use App\Models\Project;
use App\Models\User;
use App\Support\SetsWorkspaceRlsContext;
use Illuminate\Foundation\Testing\DatabaseTransactions;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Route;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\Attributes\Test;
use Symfony\Component\HttpKernel\Exception\NotFoundHttpException;
use Tests\TestCase;

/**
 * SEC-8 / SEC-10 on a real Postgres: a workspace binding that would DISARM
 * row-level security is refused, and Foundry's {slug} routes are bound.
 *
 * silver.projects.workspace_id is nullable. Callers of withWorkspaceRls()
 * pass `(string) $project->workspace_id`, so a project with no workspace
 * used to bind '' — which the fail-open policy shape (still most of the
 * cluster) reads as "every workspace".
 */
final class WorkspaceBindFailsClosedTest extends TestCase
{
    use DatabaseTransactions;

    protected function setUp(): void
    {
        parent::setUp();

        if (DB::connection()->getDriverName() !== 'pgsql') {
            $this->markTestSkipped('RLS is Postgres-only.');
        }

        // The middleware reads this; make sure the suite's own connection
        // is not mistaken for a pooled one.
        config()->set('database.connections.pgsql.pooled', false);

        Route::middleware(['web'])->get('/_test/rls/slug/{slug}', fn () => [
            'bound' => DB::selectOne("SELECT current_setting('app.workspace_id', true) AS ws")?->ws,
        ]);
    }

    /**
     * @return array<string, array{0: string}>
     */
    public static function unbindableWorkspaces(): array
    {
        return [
            'empty (a NULL workspace cast to string)' => [''],
            'not a uuid' => ['workspace-a'],
            'sql-ish' => ["' OR true --"],
        ];
    }

    #[Test]
    #[DataProvider('unbindableWorkspaces')]
    public function with_workspace_rls_refuses_a_value_that_would_disarm_rls(string $workspaceId): void
    {
        $ran = false;

        try {
            $this->binder()->run($workspaceId, function () use (&$ran): void {
                $ran = true;
            });
            $this->fail('withWorkspaceRls() accepted an unbindable workspace.');
        } catch (NotFoundHttpException) {
            // expected
        }

        $this->assertFalse($ran, 'The callback ran with RLS disarmed.');
    }

    #[Test]
    public function with_workspace_rls_binds_a_real_workspace(): void
    {
        $ws = '5ec10000-0000-4000-8000-0000000000aa';

        $this->assertSame(
            $ws,
            $this->binder()->run($ws, fn () => DB::selectOne("SELECT current_setting('app.workspace_id', true) AS ws")?->ws),
        );
    }

    #[Test]
    public function a_member_is_refused_a_project_without_a_workspace(): void
    {
        $user = User::factory()->create();
        $project = Project::factory()->create(['workspace_id' => null]);
        $user->projects()->attach($project->project_id, ['role' => 'owner']);

        // 409, matching RasterLayersController::workspaceIdOrFail().
        $this->actingAs($user)
            ->getJson("/_test/rls/slug/{$project->slug}")
            ->assertStatus(409);
    }

    #[Test]
    public function a_non_member_gets_404_for_a_project_without_a_workspace(): void
    {
        $user = User::factory()->create();
        $project = Project::factory()->create(['workspace_id' => null]);

        $this->actingAs($user)
            ->getJson("/_test/rls/slug/{$project->slug}")
            ->assertNotFound();
    }

    #[Test]
    public function a_slug_route_binds_the_projects_workspace_for_a_multi_workspace_user(): void
    {
        foreach (['5ec10000-0000-4000-8000-0000000000a1', '5ec10000-0000-4000-8000-0000000000b2'] as $ws) {
            DB::table('silver.workspaces')->insert([
                'workspace_id' => $ws,
                'name' => "SEC-8 {$ws}",
                'slug' => 'sec8-'.substr($ws, -2),
                'created_at' => now(),
                'updated_at' => now(),
            ]);
        }

        $user = User::factory()->create();
        $a = Project::factory()->create(['workspace_id' => '5ec10000-0000-4000-8000-0000000000a1']);
        $b = Project::factory()->create(['workspace_id' => '5ec10000-0000-4000-8000-0000000000b2']);
        $user->projects()->attach($a->project_id, ['role' => 'owner']);
        $user->projects()->attach($b->project_id, ['role' => 'member']);

        $this->actingAs($user)
            ->getJson("/_test/rls/slug/{$b->slug}")
            ->assertOk()
            ->assertJsonPath('bound', '5ec10000-0000-4000-8000-0000000000b2');
    }

    private function binder(): object
    {
        return new class
        {
            use SetsWorkspaceRlsContext;

            public function run(string $workspaceId, \Closure $callback): mixed
            {
                return $this->withWorkspaceRls($workspaceId, $callback);
            }
        };
    }
}
