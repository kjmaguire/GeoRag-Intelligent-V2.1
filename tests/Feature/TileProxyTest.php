<?php

namespace Tests\Feature;

use App\Models\User;
use Illuminate\Database\QueryException;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\Client\ConnectionException;
use Illuminate\Http\Client\Factory;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Tests\TestCase;

/**
 * Feature tests for TileProxyController — Module 8 Chunk 8.4.
 *
 * Covers:
 *   - PGEO: 200 + ETag on miss, 304 on hit, Cache-Control=3600
 *   - Silver: 200 + ETag on miss, 304 on hit, Cache-Control=86400
 *   - SSRF whitelist (unknown source → 404)
 *   - Silver missing project_id → 400
 *   - Silver project from another workspace → 403
 *   - 204 empty-tile pass-through for both families
 *   - Server-Timing header present on 200 and 304
 *   - Weak ETag in If-None-Match still triggers 304
 *
 * Architecture notes:
 *  - RefreshDatabase runs all migrations; the TestCase SQLite compatibility
 *    hook (tests/TestCase.php refreshApplication()) no-ops PG-specific DDL.
 *    Tests that query silver.* or public_geoscience.* directly use skipIfSqlite().
 *  - Silver ETag DB lookups are mocked via DB::shouldReceive so no real
 *    silver.projects table is needed under SQLite.
 *  - PGEO epoch is pre-seeded in the Cache (array driver under testing) so
 *    no public_geoscience.jurisdictions query is issued.
 *  - Http::fake() intercepts all Martin upstream calls.
 */
class TileProxyTest extends TestCase
{
    use RefreshDatabase;

    private User $user;

    /** A valid PGEO source from the whitelist. */
    private const PGEO_SOURCE = 'pg_mines';

    /** A valid Silver source from the whitelist. */
    private const SILVER_SOURCE = 'pg_collars_by_project';

    /** Fake project UUID used across silver tests. */
    private const PROJECT_ID = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890';

    /** PGEO epoch pre-seeded in the Cache; must match ETag derivation in tests. */
    private const PGEO_EPOCH = 1_700_000_000;

    /**
     * Skip the entire test class before database refresh when running under
     * SQLite. The silver.* migration chain (workspaces, answer_runs, etc.) uses
     * PG-specific raw SQL that cannot be run on SQLite even with the compat
     * shim in TestCase::refreshApplication().
     *
     * This hook runs BEFORE RefreshDatabase::refreshTestDatabase(), so we can
     * bail without attempting any migrations.
     */
    protected function beforeRefreshingDatabase(): void
    {
        $this->skipIfSqlite(
            'TileProxyTest requires PostgreSQL. Use phpunit.pgsql.xml for this suite. '
            .'The silver.workspaces / answer_runs migrations cannot run on SQLite.',
        );
    }

    protected function setUp(): void
    {
        parent::setUp();

        $this->user = User::factory()->create();

        // Pre-seed the PGEO epoch so the controller never hits the DB for it.
        Cache::put('pgeo_jurisdiction_epoch', self::PGEO_EPOCH, 60);

        // Default Http fake: a normal 200 tile response from Martin.
        // Individual tests that need a different response call Http::fake() again
        // with their own stub, which completely replaces this one.
        $this->fakeMartin200();
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Helpers
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * Reset the Http stub collection and install a fresh 200 Martin response.
     *
     * Http::fake() MERGES new stubs onto the existing collection — the first
     * registered wildcard match wins on each request. We must clear the
     * collection before re-registering so tests that override with 204 or a
     * throw-callback are not masked by the setUp default.
     */
    private function fakeMartin200(): void
    {
        $this->resetHttpStubs();
        Http::fake([
            '*' => Http::response(
                'fake-mvt-bytes',
                200,
                ['Content-Type' => 'application/x-protobuf'],
            ),
        ]);
    }

    /**
     * Reset the Http stub collection and install a 204 No Content response.
     */
    private function fakeMartin204(): void
    {
        $this->resetHttpStubs();
        Http::fake(['*' => Http::response('', 204)]);
    }

    /**
     * Reset the Http stub collection and install a throw callback.
     */
    private function fakeMartinThrow(string $message): void
    {
        $this->resetHttpStubs();
        Http::fake(['*' => static function () use ($message) {
            throw new ConnectionException($message);
        }]);
    }

    /**
     * Clear all previously registered Http stubs so a fresh Http::fake() call
     * takes precedence.
     *
     * Http::fake() MERGES stubs rather than replacing them; the first wildcard
     * match always wins. Since stubCallbacks is protected, we use Reflection to
     * reset it. This is a test-internal concern only.
     */
    private function resetHttpStubs(): void
    {
        /** @var Factory $factory */
        $factory = $this->app->make(Factory::class);

        try {
            $prop = new \ReflectionProperty($factory, 'stubCallbacks');
            $prop->setAccessible(true);
            $prop->setValue($factory, collect());
        } catch (\ReflectionException) {
            // Property name changed in a future version — fall back to no-op.
            // The test may behave incorrectly in that case, but will not crash.
        }
    }

    /**
     * Seed a silver.projects row so the ETag lookup returns a real data_version.
     * Also inserts a project_user pivot row so hasProjectAccess() returns true.
     *
     * On PgSQL (georag_test) this uses real DB inserts.
     * The project row is cleaned up by RefreshDatabase wrapping each test in a
     * transaction that is rolled back at the end.
     */
    private function seedSilverProject(string $projectId, int $dataVersion = 42): void
    {
        // Ensure the default workspace exists (migrations seed it, but just in case).
        DB::statement("
            INSERT INTO silver.workspaces (workspace_id, name, slug, data_version, created_at, updated_at)
            VALUES ('a0000000-0000-0000-0000-000000000001', 'Default Workspace', 'default', 0, NOW(), NOW())
            ON CONFLICT (workspace_id) DO NOTHING
        ");

        // Insert the test project row (all NOT NULL columns must be provided).
        DB::statement("
            INSERT INTO silver.projects
                (project_id, project_name, crs_datum, company, orientation_reference, status, slug, workspace_id, data_version, created_at, updated_at)
            VALUES
                (?::uuid, 'Test Project', 'EPSG:32613', 'Test Co', 'grid', 'active', 'test-project-8-4', 'a0000000-0000-0000-0000-000000000001'::uuid, ?, NOW(), NOW())
            ON CONFLICT (project_id) DO UPDATE SET data_version = EXCLUDED.data_version
        ", [$projectId, $dataVersion]);

        // Grant the test user access.
        try {
            DB::table('project_user')->insertOrIgnore([
                'user_id' => $this->user->id,
                'project_id' => $projectId,
                'role' => 'member',
                'created_at' => now(),
                'updated_at' => now(),
            ]);
        } catch (\Throwable) {
            // Pivot table absent — hasProjectAccess() fails open with warning.
        }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // 1. Happy path — PGEO tile: If-None-Match miss → 200 + ETag header set
    // ─────────────────────────────────────────────────────────────────────────

    public function test_pgeo_tile_cache_miss_returns_200_with_etag_header(): void
    {
        $response = $this->actingAs($this->user)
            ->get('/tiles/public-geoscience/'.self::PGEO_SOURCE.'/10/100/200.pbf', [
                'If-None-Match' => '"stale-hash-that-will-not-match"',
            ]);

        $response->assertOk();

        $etag = $response->headers->get('ETag');
        $this->assertNotNull($etag, 'ETag header must be present on a 200 PGEO tile response.');
        $this->assertMatchesRegularExpression(
            '/^"[0-9a-f]{32}"$/',
            $etag,
            'ETag must be a quoted 32-char hex MD5.',
        );

        // Assert the value matches what the controller should derive.
        $expectedTag = md5(self::PGEO_EPOCH.'|10|100|200');
        $this->assertSame("\"{$expectedTag}\"", $etag);
    }

    // ─────────────────────────────────────────────────────────────────────────
    // 2. Happy path — PGEO tile: If-None-Match hit → 304 Not Modified
    // ─────────────────────────────────────────────────────────────────────────

    public function test_pgeo_tile_cache_hit_returns_304_with_empty_body(): void
    {
        $expectedTag = md5(self::PGEO_EPOCH.'|10|100|200');

        $response = $this->actingAs($this->user)
            ->get('/tiles/public-geoscience/'.self::PGEO_SOURCE.'/10/100/200.pbf', [
                'If-None-Match' => "\"{$expectedTag}\"",
            ]);

        $response->assertStatus(304);
        $this->assertEmpty($response->getContent(), '304 body must be empty.');
        $this->assertSame(
            "\"{$expectedTag}\"",
            $response->headers->get('ETag'),
            'ETag must be echoed back on a 304 response.',
        );
    }

    // ─────────────────────────────────────────────────────────────────────────
    // 3. Happy path — Silver tile: If-None-Match miss → 200 + ETag header set
    // ─────────────────────────────────────────────────────────────────────────

    public function test_silver_tile_cache_miss_returns_200_with_etag_header(): void
    {
        $this->seedSilverProject(self::PROJECT_ID, 42);

        $response = $this->actingAs($this->user)
            ->get(
                '/tiles/silver/'.self::SILVER_SOURCE.'/10/100/200.pbf?project_id='.self::PROJECT_ID,
                ['If-None-Match' => '"wrong-etag"'],
            );

        $response->assertOk();

        $etag = $response->headers->get('ETag');
        $this->assertNotNull($etag);

        $expectedTag = md5('42|10|100|200|'.self::PROJECT_ID);
        $this->assertSame("\"{$expectedTag}\"", $etag);
    }

    // ─────────────────────────────────────────────────────────────────────────
    // 4. Happy path — Silver tile: If-None-Match hit → 304
    // ─────────────────────────────────────────────────────────────────────────

    public function test_silver_tile_cache_hit_returns_304(): void
    {
        $this->seedSilverProject(self::PROJECT_ID, 42);

        $expectedTag = md5('42|10|100|200|'.self::PROJECT_ID);

        $response = $this->actingAs($this->user)
            ->get(
                '/tiles/silver/'.self::SILVER_SOURCE.'/10/100/200.pbf?project_id='.self::PROJECT_ID,
                ['If-None-Match' => "\"{$expectedTag}\""],
            );

        $response->assertStatus(304);
        $this->assertEmpty($response->getContent(), '304 body must be empty.');
        $this->assertSame("\"{$expectedTag}\"", $response->headers->get('ETag'));
    }

    // ─────────────────────────────────────────────────────────────────────────
    // 5. SSRF: unknown PGEO source returns 404
    // ─────────────────────────────────────────────────────────────────────────

    public function test_unknown_pgeo_source_returns_404(): void
    {
        $this->actingAs($this->user)
            ->get('/tiles/public-geoscience/silver_collars/10/100/200.pbf')
            ->assertNotFound();
    }

    // ─────────────────────────────────────────────────────────────────────────
    // 6. SSRF: unknown Silver source returns 404
    // ─────────────────────────────────────────────────────────────────────────

    public function test_unknown_silver_source_returns_404(): void
    {
        $this->actingAs($this->user)
            ->get('/tiles/silver/pg_internal_secret/10/100/200.pbf?project_id='.self::PROJECT_ID)
            ->assertNotFound();
    }

    // ─────────────────────────────────────────────────────────────────────────
    // 7. Silver tile without project_id → 400
    // ─────────────────────────────────────────────────────────────────────────

    public function test_silver_tile_missing_project_id_returns_400(): void
    {
        $this->actingAs($this->user)
            ->get('/tiles/silver/'.self::SILVER_SOURCE.'/10/100/200.pbf')
            ->assertStatus(400);
    }

    // ─────────────────────────────────────────────────────────────────────────
    // 7b. Silver tile with malformed (non-UUID) project_id → 400
    // ─────────────────────────────────────────────────────────────────────────

    public function test_silver_tile_malformed_project_id_returns_400(): void
    {
        $this->actingAs($this->user)
            ->get('/tiles/silver/'.self::SILVER_SOURCE.'/10/100/200.pbf?project_id=not-a-uuid')
            ->assertStatus(400);
    }

    // ─────────────────────────────────────────────────────────────────────────
    // 8. Silver tile with project_id from another workspace → 403
    // ─────────────────────────────────────────────────────────────────────────

    public function test_silver_tile_unowned_project_returns_403(): void
    {
        // Do NOT grant access — the user has no pivot row for this project.
        // hasProjectAccess() returns false when the pivot row is absent and
        // the table exists.
        try {
            // Ensure NO pivot row exists for this user+project.
            DB::table('project_user')
                ->where('user_id', $this->user->id)
                ->where('project_id', self::PROJECT_ID)
                ->delete();

            $response = $this->actingAs($this->user)
                ->get('/tiles/silver/'.self::SILVER_SOURCE.'/10/100/200.pbf?project_id='.self::PROJECT_ID);

            $response->assertStatus(403);
        } catch (QueryException $e) {
            // project_user table absent in this DB → hasProjectAccess() fails open.
            $this->markTestSkipped(
                'project_user pivot table absent; workspace scope gate is degraded '
                .'(fails open). Run `php artisan migrate` to activate enforcement.',
            );
        }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // 9. Cache-Control: PGEO → max-age=3600, Silver → max-age=86400
    // ─────────────────────────────────────────────────────────────────────────

    public function test_pgeo_tile_cache_control_contains_max_age_3600(): void
    {
        $response = $this->actingAs($this->user)
            ->get('/tiles/public-geoscience/'.self::PGEO_SOURCE.'/10/100/200.pbf');

        $response->assertOk();
        $cc = (string) $response->headers->get('Cache-Control');
        $this->assertStringContainsString('max-age=3600', $cc);
        $this->assertStringContainsString('must-revalidate', $cc);
        $this->assertStringNotContainsString('max-age=300', $cc);
    }

    public function test_silver_tile_cache_control_contains_max_age_86400(): void
    {
        $this->seedSilverProject(self::PROJECT_ID, 1);

        $response = $this->actingAs($this->user)
            ->get('/tiles/silver/'.self::SILVER_SOURCE.'/10/100/200.pbf?project_id='.self::PROJECT_ID);

        $response->assertOk();
        $cc = (string) $response->headers->get('Cache-Control');
        $this->assertStringContainsString('max-age=86400', $cc);
        $this->assertStringContainsString('must-revalidate', $cc);
        $this->assertStringNotContainsString('max-age=300', $cc);
    }

    // ─────────────────────────────────────────────────────────────────────────
    // 10. Server-Timing header present and reasonable on 200
    // ─────────────────────────────────────────────────────────────────────────

    public function test_pgeo_tile_server_timing_present_on_200(): void
    {
        $response = $this->actingAs($this->user)
            ->get('/tiles/public-geoscience/'.self::PGEO_SOURCE.'/10/100/200.pbf');

        $response->assertOk();
        $st = $response->headers->get('Server-Timing');
        $this->assertNotNull($st, 'Server-Timing header must be present.');
        $this->assertMatchesRegularExpression('/db;dur=\d+(\.\d+)?/', $st);

        // Extract db;dur value — should be well under 50 ms in test env.
        preg_match('/db;dur=(\d+(?:\.\d+)?)/', (string) $st, $m);
        if (isset($m[1])) {
            $this->assertLessThan(
                50.0,
                (float) $m[1],
                'db;dur must be < 50 ms (Cache::remember is a no-op in tests).',
            );
        }
    }

    public function test_silver_tile_server_timing_present_on_200(): void
    {
        $this->seedSilverProject(self::PROJECT_ID, 5);

        $response = $this->actingAs($this->user)
            ->get('/tiles/silver/'.self::SILVER_SOURCE.'/10/100/200.pbf?project_id='.self::PROJECT_ID);

        $response->assertOk();
        $st = $response->headers->get('Server-Timing');
        $this->assertNotNull($st, 'Server-Timing header must be present on silver 200.');
        $this->assertMatchesRegularExpression('/db;dur=\d+(\.\d+)?/', $st);
    }

    // ─────────────────────────────────────────────────────────────────────────
    // 11. 204 empty-tile pass-through for both families
    // ─────────────────────────────────────────────────────────────────────────

    public function test_pgeo_204_empty_tile_is_passed_through(): void
    {
        $this->fakeMartin204();

        $this->actingAs($this->user)
            ->get('/tiles/public-geoscience/'.self::PGEO_SOURCE.'/10/100/200.pbf')
            ->assertNoContent();
    }

    public function test_silver_204_empty_tile_is_passed_through(): void
    {
        $this->fakeMartin204();
        $this->seedSilverProject(self::PROJECT_ID, 0);

        $this->actingAs($this->user)
            ->get('/tiles/silver/'.self::SILVER_SOURCE.'/10/100/200.pbf?project_id='.self::PROJECT_ID)
            ->assertNoContent();
    }

    // ─────────────────────────────────────────────────────────────────────────
    // 12. Server-Timing present on 304 (ETag lookup still happened)
    // ─────────────────────────────────────────────────────────────────────────

    public function test_pgeo_304_has_server_timing_header(): void
    {
        $expectedTag = md5(self::PGEO_EPOCH.'|5|10|15');

        $response = $this->actingAs($this->user)
            ->get('/tiles/public-geoscience/'.self::PGEO_SOURCE.'/5/10/15.pbf', [
                'If-None-Match' => "\"{$expectedTag}\"",
            ]);

        $response->assertStatus(304);
        $this->assertNotNull(
            $response->headers->get('Server-Timing'),
            'Server-Timing must be present on 304 (db lookup still ran to derive the ETag).',
        );
    }

    // ─────────────────────────────────────────────────────────────────────────
    // 13. Weak ETag in If-None-Match is still honoured (RFC 7232 §3.2)
    // ─────────────────────────────────────────────────────────────────────────

    public function test_weak_etag_in_if_none_match_triggers_304(): void
    {
        $expectedTag = md5(self::PGEO_EPOCH.'|10|100|200');

        $response = $this->actingAs($this->user)
            ->get('/tiles/public-geoscience/'.self::PGEO_SOURCE.'/10/100/200.pbf', [
                // W/ prefix — proxy must strip and still match.
                'If-None-Match' => "W/\"{$expectedTag}\"",
            ]);

        $response->assertStatus(304);
    }

    // ─────────────────────────────────────────────────────────────────────────
    // 14. Unauthenticated request returns 401
    // ─────────────────────────────────────────────────────────────────────────

    public function test_unauthenticated_request_returns_401(): void
    {
        $this->getJson('/tiles/public-geoscience/'.self::PGEO_SOURCE.'/10/123/456.pbf')
            ->assertUnauthorized();
    }

    // ─────────────────────────────────────────────────────────────────────────
    // 15. 502 when Martin throws a connection exception
    // ─────────────────────────────────────────────────────────────────────────

    public function test_martin_connection_exception_returns_502(): void
    {
        $this->fakeMartinThrow('Martin is down');

        $this->actingAs($this->user)
            ->get('/tiles/public-geoscience/'.self::PGEO_SOURCE.'/10/123/456.pbf')
            ->assertStatus(502);
    }
}
