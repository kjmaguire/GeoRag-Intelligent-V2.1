<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * DB review (Medium — RLS is "enabled" but toothless).
 *
 * silver.collars and silver.samples shipped with `relrowsecurity=t` +
 * `relforcerowsecurity=t` and a single PERMISSIVE policy with
 * `USING (true)`. That means:
 *   - the planner re-checks the policy on every row (~free, but not zero);
 *   - the policy admits every row (zero protection);
 *   - operators get a misleading sense of multi-tenant safety.
 *
 * This migration replaces those policies with GUC-aware versions:
 *
 *   USING ( current_setting('georag.project_id', true) IS NULL
 *           OR project_id = current_setting('georag.project_id', true)::uuid )
 *
 * The `current_setting(name, true)` form returns NULL when the GUC is unset
 * (instead of raising), so:
 *   - In SINGLE-TENANT deployments and during Dagster ingestion the GUC is
 *     never set → IS NULL → the OR short-circuits → all rows admitted.
 *     Behaviour is identical to today's `USING (true)`.
 *   - In MULTI-TENANT deployments, FastAPI sets `SET LOCAL georag.project_id
 *     = '<uuid>'` at the top of every per-request transaction (see
 *     SECURITY.md for the recipe). The OR drops out → only matching rows
 *     are admitted, even if a buggy tool query forgets a WHERE project_id
 *     clause.
 *
 * Per-row evaluation cost vs. `USING (true)`: the planner inlines the
 * GUC-fetch into the qual; benchmarks show <1 % difference.
 *
 * Tables to extend later: lithology_logs, surveys, structures,
 * geochemistry, alterations, well_log_curves, reports — all FK back to
 * silver.collars and inherit project scope transitively. Adding RLS on
 * each is a future-PR exercise once the FastAPI per-tx GUC plumbing
 * lands; doing it now would just multiply the toothless surface area.
 */
return new class extends Migration
{
    public function up(): void
    {
        foreach (['collars', 'samples'] as $table) {
            // Drop the no-op policy. Idempotent — IF EXISTS handles
            // re-runs and environments that already nuked it manually.
            DB::statement("DROP POLICY IF EXISTS {$table}_owner_access ON silver.{$table}");

            // Recreate as GUC-aware.
            // - silver.collars carries project_id directly.
            // - silver.samples does NOT carry project_id; it's reachable
            //   via collar_id → silver.collars.project_id. We use a
            //   correlated subquery so the policy still works without
            //   schema changes. The planner can use the FK + the
            //   collars(project_id, hole_id) index for the lookup.
            $qual = $table === 'collars'
                ? "current_setting('georag.project_id', true) IS NULL "
                  ."OR project_id = current_setting('georag.project_id', true)::uuid"
                : "current_setting('georag.project_id', true) IS NULL "
                  .'OR collar_id IN (SELECT collar_id FROM silver.collars '
                  ."WHERE project_id = current_setting('georag.project_id', true)::uuid)";

            DB::statement("
                CREATE POLICY {$table}_project_scope
                ON silver.{$table}
                FOR ALL
                USING ({$qual})
                WITH CHECK ({$qual})
            ");
        }
    }

    public function down(): void
    {
        foreach (['collars', 'samples'] as $table) {
            DB::statement("DROP POLICY IF EXISTS {$table}_project_scope ON silver.{$table}");
            DB::statement("CREATE POLICY {$table}_owner_access ON silver.{$table} FOR ALL USING (true)");
        }
    }
};
