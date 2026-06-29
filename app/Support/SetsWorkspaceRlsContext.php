<?php

declare(strict_types=1);

namespace App\Support;

use Illuminate\Support\Facades\DB;

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
        return DB::transaction(function () use ($workspaceId, $callback) {
            DB::statement("SELECT set_config('app.workspace_id', ?, true)", [$workspaceId]);

            return $callback();
        });
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

        DB::statement("SELECT set_config('app.workspace_id', ?, true)", [$workspaceId]);
    }
}
