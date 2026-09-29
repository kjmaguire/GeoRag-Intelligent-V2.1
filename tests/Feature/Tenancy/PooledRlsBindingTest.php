<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use App\Support\SetsWorkspaceRlsContext;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Route;
use PDO;
use PHPUnit\Framework\Attributes\Test;
use Tests\TestCase;
use Throwable;

/**
 * SEC-2 — what a transaction-mode pooler does to `app.workspace_id`, proved
 * against a real PgBouncer rather than asserted from its documentation.
 *
 * Runs only when RLS_POOLER_PORT points at a PgBouncer in TRANSACTION mode
 * with `default_pool_size = 1` for the test database, in front of the same
 * Postgres the pgsql suite uses (RLS_POOLER_HOST defaults to DB_HOST). A pool
 * of one makes "the next client lands on the same backend" deterministic
 * instead of likely. CI has no PgBouncer, so there it skips; the pooler-free
 * half of the contract (BindWorkspaceRlsContext refusing under DB_POOLED) is
 * pinned in tests/Feature/Middleware/BindWorkspaceRlsContextTest.php.
 *
 *   pgbouncer.ini:  pool_mode = transaction, default_pool_size = 1,
 *                   server_reset_query = DISCARD ALL (compose's setting —
 *                   which transaction mode never runs)
 *   run:            RLS_POOLER_PORT=6432 php vendor/bin/phpunit \
 *                     -c phpunit.pgsql.xml tests/Feature/Tenancy/PooledRlsBindingTest.php
 */
final class PooledRlsBindingTest extends TestCase
{
    private const WORKSPACE_A = '5ec20000-0000-4000-8000-00000000000a';

    private const WORKSPACE_B = '5ec20000-0000-4000-8000-00000000000b';

    private const PROBE_TABLE = 'bronze.raw_surveys';

    private const PROBE_HOLE = 'SEC2-POOLER-PROBE';

    protected function setUp(): void
    {
        parent::setUp();

        if (DB::connection()->getDriverName() !== 'pgsql') {
            $this->markTestSkipped('RLS is Postgres-only.');
        }

        if ((string) getenv('RLS_POOLER_PORT') === '') {
            $this->markTestSkipped('RLS_POOLER_PORT is not set — no transaction-mode PgBouncer to test against.');
        }

        $pooled = config('database.connections.pgsql');
        $pooled['host'] = (string) (getenv('RLS_POOLER_HOST') ?: $pooled['host']);
        $pooled['port'] = (string) getenv('RLS_POOLER_PORT');
        // PgBouncer rejects startup parameters it does not know; search_path
        // is set per statement below rather than at connect time.
        unset($pooled['search_path']);
        config()->set('database.connections.pgsql_pooled', $pooled);
    }

    protected function tearDown(): void
    {
        if (DB::connection()->getDriverName() === 'pgsql') {
            try {
                DB::connection('pgsql')->delete(
                    'DELETE FROM '.self::PROBE_TABLE.' WHERE hole_id = ?',
                    [self::PROBE_HOLE],
                );
            } catch (Throwable) {
                // Table absent — nothing was inserted.
            }
            DB::purge('pgsql_pooled');
        }

        parent::tearDown();
    }

    /**
     * The hazard, observed: a session-scoped set_config() made by one client
     * is still in force for a DIFFERENT client that the pooler hands the same
     * backend. This is exactly what BindWorkspaceRlsContext's per-request
     * `set_config(..., false)` would do behind PgBouncer — tenant X's
     * binding scoping tenant Y's next request — and why it now refuses to run
     * there instead of logging.
     */
    #[Test]
    public function a_session_guc_from_one_client_is_visible_to_the_next_client(): void
    {
        $tenantX = $this->poolerClient();
        $tenantX->query("SELECT set_config('app.workspace_id', '".self::WORKSPACE_A."', false)");
        $tenantX = null; // disconnect: the server backend goes back to the pool

        $tenantY = $this->poolerClient();
        $seen = $tenantY->query("SELECT current_setting('app.workspace_id', true)")->fetchColumn();

        $this->assertSame(
            self::WORKSPACE_A,
            $seen,
            'Expected the pooled backend to carry the previous client\'s session GUC. If this fails, '
            .'the pooler is not in transaction mode with a pool of one, and the test proves nothing.',
        );

        // Leave the backend clean for the next test.
        $tenantY->query("SELECT set_config('app.workspace_id', '', false)");
    }

    /**
     * The safe shape: SET LOCAL inside a transaction dies at COMMIT, so the
     * next client on the same backend inherits nothing.
     */
    #[Test]
    public function a_transaction_scoped_guc_does_not_outlive_its_transaction(): void
    {
        $this->poolerClient()->query("SELECT set_config('app.workspace_id', '', false)");

        $tenantX = $this->poolerClient();
        $tenantX->beginTransaction();
        $tenantX->query("SELECT set_config('app.workspace_id', '".self::WORKSPACE_A."', true)");
        $this->assertSame(
            self::WORKSPACE_A,
            $tenantX->query("SELECT current_setting('app.workspace_id', true)")->fetchColumn(),
        );
        $tenantX->commit();
        $tenantX = null;

        $tenantY = $this->poolerClient();
        $seen = $tenantY->query("SELECT COALESCE(current_setting('app.workspace_id', true), '')")->fetchColumn();

        $this->assertSame('', $seen, 'SET LOCAL leaked past COMMIT to the next pooled client.');
    }

    /**
     * End to end through Laravel: withWorkspaceRls() on a connection that goes
     * through the pooler reads only its own tenant's rows from a fail-closed
     * table, as the non-superuser application role, and leaves nothing behind.
     */
    #[Test]
    public function with_workspace_rls_scopes_reads_through_the_pooler(): void
    {
        $table = self::PROBE_TABLE;
        if (DB::selectOne('SELECT to_regclass(?) IS NOT NULL AS present', [$table])?->present !== true) {
            $this->markTestSkipped("{$table} is absent from this cluster.");
        }

        // Committed via the direct connection as the owner, so the pooled
        // transaction below can see them. tearDown() removes them.
        DB::connection('pgsql')->insert(
            "INSERT INTO {$table} (workspace_id, hole_id, depth, raw_row)
             VALUES (?::uuid, ?, 10, '{}'::jsonb), (?::uuid, ?, 20, '{}'::jsonb)",
            [self::WORKSPACE_A, self::PROBE_HOLE, self::WORKSPACE_B, self::PROBE_HOLE],
        );

        $default = config('database.default');
        config()->set('database.default', 'pgsql_pooled');

        try {
            $binder = new class
            {
                use SetsWorkspaceRlsContext;

                /**
                 * @return list<string>
                 */
                public function workspacesVisibleTo(string $workspaceId, string $table, string $hole): array
                {
                    return $this->withWorkspaceRls($workspaceId, function () use ($table, $hole): array {
                        DB::statement('SET LOCAL ROLE georag_app');

                        return array_map(
                            static fn (object $r): string => (string) $r->workspace_id,
                            DB::select("SELECT workspace_id::text AS workspace_id FROM {$table} WHERE hole_id = ?", [$hole]),
                        );
                    });
                }
            };

            $this->assertSame(
                [self::WORKSPACE_A],
                $binder->workspacesVisibleTo(self::WORKSPACE_A, $table, self::PROBE_HOLE),
            );
            $this->assertSame(
                [self::WORKSPACE_B],
                $binder->workspacesVisibleTo(self::WORKSPACE_B, $table, self::PROBE_HOLE),
            );
        } finally {
            config()->set('database.default', $default);
            DB::purge('pgsql_pooled');
        }

        // A fresh pooled client, no bind of its own, as the app role: the
        // fail-closed policy returns nothing — nothing leaked from above.
        $next = $this->poolerClient();
        $next->beginTransaction();
        $next->query('SET LOCAL ROLE georag_app');
        $stmt = $next->prepare("SELECT count(*) FROM {$table} WHERE hole_id = ?");
        $stmt->execute([self::PROBE_HOLE]);
        $this->assertSame(0, (int) $stmt->fetchColumn(), 'A later pooled client inherited a workspace binding.');
        $next->rollBack();
    }

    /**
     * And the middleware, on a real Postgres connection flagged as pooled:
     * refused before any controller runs.
     */
    #[Test]
    public function the_request_middleware_refuses_to_serve_behind_a_pooler(): void
    {
        config()->set('database.connections.pgsql.pooled', true);

        $reached = false;
        Route::middleware(['api'])->get('/_test/rls/pooled', function (Request $r) use (&$reached): array {
            $reached = true;

            return ['workspace_id' => $r->attributes->get('workspace_id')];
        });

        $this->getJson('/_test/rls/pooled')->assertStatus(503);
        $this->assertFalse($reached, 'The controller ran behind a pooler with a session-scoped RLS bind.');
    }

    private function poolerClient(): PDO
    {
        /** @var array{host: string, port: string, database: string, username: string, password: string} $c */
        $c = config('database.connections.pgsql_pooled');

        return new PDO(
            "pgsql:host={$c['host']};port={$c['port']};dbname={$c['database']}",
            $c['username'],
            $c['password'],
            [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION],
        );
    }
}
