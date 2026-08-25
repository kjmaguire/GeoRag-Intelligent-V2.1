<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * The 30 seeded rock codes were invisible to every real tenant.
 *
 * `2026_05_20_060900_rock_codes_dual_system_and_seed` writes its NRCAN + GSC
 * lookup under
 *
 *     SEED_WORKSPACE_ID = '00000000-0000-0000-0000-000000000001'
 *
 * — a placeholder that is not a row in `silver.workspaces`. The live tenants
 * are `a0000000-…-0001` and `f0f0f0f0-…-0001`, and the RLS policy on
 * `silver.rock_codes` is the fail-CLOSED shape
 *
 *     USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
 *
 * so a scoped read matches nothing. Measured against the live Azure database
 * on 2026-08-25: 30 rows present with the GUC unset, **0 rows** with it bound
 * to the workspace every project in the system belongs to.
 *
 * What that cost: `rock_codes` is the lookup that turns a logged lithology
 * code ('bslt') into a name and a display colour. Every join to it returned
 * NULL, so strip logs and the 3D lithology view had codes where they should
 * have had rock names — silently, because a LEFT JOIN that finds nothing is
 * not an error.
 *
 * The fix is to REPLICATE, not to move. `rock_codes` is workspace-scoped on
 * purpose (§04e — a workspace may extend the standard systems with its own
 * codes), so each tenant gets its own copy of the standard set and stays free
 * to add to it. Idempotent: the unique index on
 * `(workspace_id, system, code)` plus ON CONFLICT DO NOTHING means re-running
 * writes nothing and a workspace that has already customised a code keeps its
 * version.
 *
 * The placeholder rows are deliberately left in place. Deleting them would
 * make the original migration's `down()` a no-op and buys nothing — they are
 * invisible to every tenant, which is the entire complaint.
 */
return new class extends Migration
{
    private const SEED_WORKSPACE_ID = '00000000-0000-0000-0000-000000000001';

    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // Guarded so a fresh cluster whose seed migration has not run yet —
        // or a test database built from a partial schema — does not fail the
        // whole migration batch on a table that is simply not there.
        $exists = DB::selectOne(
            "SELECT to_regclass('silver.rock_codes') AS t",
        );
        if ($exists === null || $exists->t === null) {
            return;
        }

        DB::statement(
            <<<'SQL'
            INSERT INTO silver.rock_codes (workspace_id, system, code, name, description)
            SELECT w.workspace_id, r.system, r.code, r.name, r.description
              FROM silver.rock_codes r
             CROSS JOIN silver.workspaces w
             WHERE r.workspace_id = ?::uuid
               AND w.workspace_id <> ?::uuid
            ON CONFLICT DO NOTHING
            SQL,
            [self::SEED_WORKSPACE_ID, self::SEED_WORKSPACE_ID],
        );
    }

    public function down(): void
    {
        // Intentionally irreversible.
        //
        // Rolling this back means deleting rock codes out of live workspaces,
        // and by then there is no way to tell a replicated standard code from
        // one a geologist edited or added — they are the same shape. Leaving
        // the reference data in place is the safe direction.
    }
};
