<?php

declare(strict_types=1);

namespace Tests\Feature;

use App\Models\User;
use App\Services\Ingestion\WorkspaceDataVersionBumper;
use App\Support\Tiles\SilverTileContextEpoch;
use Illuminate\Contracts\Redis\Factory as RedisFactory;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Testing\TestResponse;
use Mockery;
use Tests\TestCase;

/**
 * A silver tile used to pay three lookups (membership, workspace, data_version)
 * before Martin was asked. They now come from one query cached per (user,
 * project) for 60 s, namespaced by an epoch the data_version bump advances.
 *
 * PostgreSQL only: silver.projects / project_user.
 *   php artisan test -c phpunit.pgsql.xml --filter=TileProxySilverContextTest
 */
final class TileProxySilverContextTest extends TestCase
{
    use RefreshDatabase;

    private const SOURCE = 'pg_collars_by_project';

    private const PROJECT_ID = 'b1b2c3d4-e5f6-7890-abcd-ef1234567891';

    private const WORKSPACE_ID = 'a0000000-0000-0000-0000-000000000001';

    private User $user;

    protected function beforeRefreshingDatabase(): void
    {
        $this->skipIfSqlite('Requires PostgreSQL (silver.projects).');
    }

    protected function setUp(): void
    {
        parent::setUp();

        $this->user = User::factory()->create();
        Cache::flush();
        Http::fake(['*' => Http::response('fake-mvt-bytes', 200, ['Content-Type' => 'application/x-protobuf'])]);

        DB::statement("
            INSERT INTO silver.workspaces (workspace_id, name, slug, data_version, created_at, updated_at)
            VALUES (?::uuid, 'Default Workspace', 'default', 0, NOW(), NOW())
            ON CONFLICT (workspace_id) DO NOTHING
        ", [self::WORKSPACE_ID]);
        DB::statement("
            INSERT INTO silver.projects
                (project_id, project_name, crs_datum, company, orientation_reference, status, slug, workspace_id, data_version, created_at, updated_at)
            VALUES (?::uuid, 'Ctx Project', 'EPSG:32613', 'Co', 'grid', 'active', 'ctx-project-1', ?::uuid, 7, NOW(), NOW())
        ", [self::PROJECT_ID, self::WORKSPACE_ID]);
    }

    private function grant(User $user): void
    {
        DB::table('project_user')->insert([
            'user_id' => $user->id,
            'project_id' => self::PROJECT_ID,
            'role' => 'member',
            'created_at' => now(),
            'updated_at' => now(),
        ]);
    }

    private function tile(User $user, int $x = 100): TestResponse
    {
        return $this->actingAs($user)->get('/tiles/silver/'.self::SOURCE."/10/{$x}/200.pbf?project_id=".self::PROJECT_ID);
    }

    /**
     * Run $fn and return the SQL of every statement the tile controller itself
     * issues for the project context (membership + workspace + data_version).
     * BindWorkspaceRlsContext runs its own workspace lookup on every request
     * and is deliberately not counted here.
     *
     * @return list<string>
     */
    private function projectQueries(callable $fn): array
    {
        $seen = [];
        DB::listen(function ($query) use (&$seen): void {
            if (str_contains($query->sql, 'data_version')) {
                $seen[] = $query->sql;
            }
        });
        $fn();

        return $seen;
    }

    public function test_the_first_tile_costs_one_lookup_and_later_tiles_cost_none(): void
    {
        $this->grant($this->user);

        $first = $this->projectQueries(fn () => $this->tile($this->user, 100)->assertOk());
        $second = $this->projectQueries(fn () => $this->tile($this->user, 101)->assertOk());

        $this->assertCount(1, $first, 'membership + workspace + data_version must be a single query');
        $this->assertSame([], $second, 'a pan burst must be served from the cached context');
    }

    public function test_the_etag_and_workspace_scope_still_come_from_the_project_row(): void
    {
        $this->grant($this->user);

        $response = $this->tile($this->user)->assertOk();

        $this->assertSame('"'.md5('7|10|100|200|'.self::PROJECT_ID).'"', $response->headers->get('ETag'));
        Http::assertSent(fn ($request): bool => str_contains($request->url(), 'workspace_id='.self::WORKSPACE_ID)
            && str_contains($request->url(), 'project_id='.self::PROJECT_ID));
    }

    public function test_a_non_member_is_denied_and_the_denial_is_not_cached(): void
    {
        $this->tile($this->user)->assertForbidden();

        // Added to the project a moment later: must work at once, not after the TTL.
        $this->grant($this->user);
        $this->tile($this->user)->assertOk();
    }

    public function test_one_users_cached_context_never_authorises_another_user(): void
    {
        $member = $this->user;
        $outsider = User::factory()->create();
        $this->grant($member);

        $this->tile($member)->assertOk();
        $this->tile($outsider)->assertForbidden();
    }

    public function test_advancing_the_epoch_makes_the_next_tile_see_the_new_data_version(): void
    {
        $this->grant($this->user);
        $this->tile($this->user)->assertOk();

        DB::table('silver.projects')->where('project_id', self::PROJECT_ID)->update(['data_version' => 8]);

        // Still inside the 60 s window: the cached context is served.
        $this->assertSame('"'.md5('7|10|100|200|'.self::PROJECT_ID).'"', $this->tile($this->user)->headers->get('ETag'));

        SilverTileContextEpoch::advance(self::PROJECT_ID);

        $this->assertSame('"'.md5('8|10|100|200|'.self::PROJECT_ID).'"', $this->tile($this->user)->headers->get('ETag'));
    }

    public function test_a_committed_data_version_bump_advances_the_epoch(): void
    {
        $before = SilverTileContextEpoch::current(self::PROJECT_ID);

        $connection = Mockery::mock();
        $connection->shouldReceive('set')->andReturn(true);
        $factory = Mockery::mock(RedisFactory::class);
        $factory->shouldReceive('connection')->andReturn($connection);

        $result = (new WorkspaceDataVersionBumper($factory))->bump(self::WORKSPACE_ID, self::PROJECT_ID, 'run-'.uniqid());

        $this->assertTrue($result['bumped']);
        $this->assertSame(8, $result['project_version']);
        $this->assertNotSame($before, SilverTileContextEpoch::current(self::PROJECT_ID));
    }
}
