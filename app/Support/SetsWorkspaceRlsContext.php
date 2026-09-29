<?php

declare(strict_types=1);

namespace App\Support;

use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;
use Symfony\Component\HttpKernel\Exception\NotFoundHttpException;

/**
 * Set the `app.workspace_id` Postgres GUC so RLS policies on silver / gold
 * tables filter by tenant explicitly, rather than relying on the permissive
 * NULL-GUC fallback.
 *
 * Why this exists: the RLS policies on phase0-owned tables (silver.spatial_features,
 * silver.collars, gold.cross_section_panels, etc.) all check
 * `current_setting('app.workspace_id', true)`. With `true` as the second arg,
 * the policy permits NULL — meaning a request that never explicitly set the
 * GUC sees ALL workspaces. Controllers must call this method on every request
 * to remove that permissive fallback.
 *
 * Octane-safe: each request's `DB::statement('SET LOCAL ...')` is scoped to
 * the current PG transaction. PgBouncer transaction-mode pooling resets the
 * GUC at transaction boundaries — but the Laravel/PHP request lifecycle
 * keeps the transaction open for the duration of the controller call, so
 * the GUC persists across queries within one request.
 */
trait SetsWorkspaceRlsContext
{
    /**
     * Run $callback inside a DB transaction with `app.workspace_id` bound via
     * SET LOCAL, so RLS policies on silver/gold tables filter to this tenant.
     *
     * SET LOCAL (`set_config(..., true)`) inside an explicit transaction is
     * REQUIRED under PgBouncer transaction-mode pooling: only within one
     * transaction are all statements guaranteed the same backend connection,
     * and the GUC is auto-discarded at COMMIT/ROLLBACK so it can never leak to
     * the next request that reuses the pooled connection.
     *
     * Audit 2026-06-27 (C2): the previous `set_config(..., false)` form was
     * session-scoped — under transaction pooling it both failed to apply
     * reliably (each autocommit statement could land on a different backend)
     * and leaked the workspace GUC across requests. Always use this wrapper.
     *
     * @template T
     *
     * @param \Closure():T $callback
     *
     * @return T
     */
    protected function withWorkspaceRls(string $workspaceId, \Closure $callback): mixed
    {
        $this->assertBindableWorkspace($workspaceId);

        return DB::transaction(function () use ($workspaceId, $callback) {
            DB::statement("SELECT set_config('app.workspace_id', ?, true)", [$workspaceId]);

            return $callback();
        });
    }

    /**
     * Run one OPTIONAL query in its own savepoint and return $default if it
     * fails.
     *
     * Why a savepoint (LAR-4, 2026-09-29): withWorkspaceRls() holds one
     * transaction for the whole action, and on Postgres the first failed
     * statement aborts it (25P02). A bare `try { ... } catch` around a panel
     * query swallowed the error but left the transaction dead, so every later
     * statement in the request failed as well — later panels rendered empty
     * and any unguarded query 500'd the page. Rolling back to a savepoint
     * discards only this query's failure; the GUC bound before it survives.
     * SQLite never aborts a transaction on error, so only the pgsql suite can
     * see the difference (tests/Feature/Foundry/OptionalPanelSavepointTest).
     *
     * @template T
     * @template D
     *
     * @param \Closure():T $query
     * @param D $default
     *
     * @return T|D
     */
    protected function optionalQuery(\Closure $query, mixed $default): mixed
    {
        $savepoint = $this->openSavepoint();

        try {
            $result = $query();
            $this->releaseSavepoint($savepoint);

            return $result;
        } catch (\Throwable $e) {
            $this->rollBackToSavepoint($savepoint);
            Log::debug('optional panel query failed; using fallback', ['error' => $e->getMessage()]);

            return $default;
        }
    }

    /**
     * Open a savepoint for an optional block that cannot be expressed as one
     * closure (it assigns several locals). Returns the transaction level to
     * hand back to {@see releaseSavepoint()} on success or
     * {@see rollBackToSavepoint()} in the catch.
     *
     * Usage — the three calls always travel together:
     *
     *     $sp = $this->openSavepoint();
     *     try {
     *         ...
     *         $this->releaseSavepoint($sp);
     *     } catch (\Throwable $e) {
     *         $this->rollBackToSavepoint($sp);
     *     }
     *
     * Octane: holds no state; the level lives in the caller's local.
     */
    protected function openSavepoint(): int
    {
        $level = DB::transactionLevel();
        DB::beginTransaction();

        return $level;
    }

    /**
     * Pop the savepoint opened by {@see openSavepoint()}. At an outer level
     * of 0 this is a real COMMIT of a transaction the savepoint itself began.
     */
    protected function releaseSavepoint(int $level): void
    {
        if (DB::transactionLevel() > $level) {
            DB::commit();
        }
    }

    /**
     * Roll back to the level captured by {@see openSavepoint()} —
     * `ROLLBACK TO SAVEPOINT` inside an outer transaction — leaving
     * everything bound before it (the workspace GUC in particular) intact.
     * A no-op when the savepoint was never opened or was already released.
     */
    protected function rollBackToSavepoint(int $level): void
    {
        if (DB::transactionLevel() > $level) {
            DB::rollBack($level);
        }
    }

    /**
     * Imperatively bind the workspace GUC for the CURRENT transaction.
     *
     * @deprecated Unsafe outside a transaction under PgBouncer transaction
     * pooling — prefer {@see withWorkspaceRls()}. Retained only for callers
     * that already manage their own transaction; throws if none is active so
     * the fail-open footgun can never recur silently.
     */
    protected function setWorkspaceRlsContext(string $workspaceId): void
    {
        if (DB::transactionLevel() < 1) {
            throw new \RuntimeException(
                'setWorkspaceRlsContext() requires an active transaction under '
                .'PgBouncer transaction pooling. Use withWorkspaceRls() instead.',
            );
        }

        $this->assertBindableWorkspace($workspaceId);

        DB::statement("SELECT set_config('app.workspace_id', ?, true)", [$workspaceId]);
    }

    /**
     * Refuse to "arm" RLS with a value that disarms it (SEC-10).
     *
     * silver.projects.workspace_id is nullable, and callers pass
     * `(string) $project->workspace_id` — so a project with no workspace used
     * to bind '', which the fail-open policy shape reads as "every
     * workspace". That is a 404 for the request, not a quietly unscoped read.
     * Postgres only: SQLite has no RLS, and the suite's factory projects
     * carry no workspace. The driver is read from config, not the resolved
     * connection, for the reason BindWorkspaceRlsContext::bind() gives: some
     * suites swap the DatabaseManager for a mock.
     */
    private function assertBindableWorkspace(string $workspaceId): void
    {
        $connection = (string) config('database.default');
        if ((string) config("database.connections.{$connection}.driver") !== 'pgsql') {
            return;
        }

        if (preg_match('/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i', $workspaceId) !== 1) {
            throw new NotFoundHttpException(
                'Refusing to bind app.workspace_id to a missing or malformed workspace; '
                .'an empty binding disarms row-level security.',
            );
        }
    }
}
