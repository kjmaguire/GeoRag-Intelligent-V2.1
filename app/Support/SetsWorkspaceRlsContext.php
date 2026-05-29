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
     * Pin the Postgres session-level workspace GUC for the current request.
     *
     * Callers MUST be inside a transaction or this will silently no-op
     * under PgBouncer transaction-mode. Wrap the controller body in a
     * transaction if you depend on this for RLS isolation.
     */
    protected function setWorkspaceRlsContext(string $workspaceId): void
    {
        // SET LOCAL only applies inside an explicit transaction. To remain
        // useful in Laravel's default auto-commit mode (one statement per
        // transaction), we use the function-call form which works outside
        // explicit BEGIN/COMMIT as well — set_config(..., false) is session-
        // scoped (not transaction-scoped), persisting across the request's
        // queries on the same connection.
        DB::statement("SELECT set_config('app.workspace_id', ?, false)", [$workspaceId]);
    }
}
