<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * SECURITY FIX 2026-05-28 — close the last broken-GUC RLS policy
 * missed by the 2026-05-25 / 2026-05-29 sweeps.
 *
 * **The bug.** `gold.repair_shadow_daily.repair_shadow_daily_workspace_isolation`
 * still uses `current_setting('georag.workspace_id', true)` in both the
 * USING and WITH CHECK clauses. The app sets `app.workspace_id` on every
 * canonical codepath, so the legacy GUC is empty → the text comparison
 * resolves to FALSE for every row, but the SHAPE here has no
 * `NULLIF(...) IS NULL OR ...` guard at all, so:
 *
 *   - SELECT: fail-CLOSED for every legitimate runtime caller (no rows)
 *   - INSERT/UPDATE: fail-CLOSED via WITH CHECK
 *
 * Net effect under production conditions today: the aggregate workflow
 * sets `georag.workspace_id` itself (see
 * src/fastapi/app/hatchet_workflows/repair_shadow_aggregate.py L328),
 * which keeps that workflow working, but ANY external query against
 * gold.repair_shadow_daily (Grafana, ad-hoc analyst, the Phase 10
 * agents that set `app.workspace_id`) sees zero rows because the policy
 * checks the wrong GUC.
 *
 * **Why the test missed it.** `gold.repair_shadow_daily` is created
 * lazily by the Hatchet workflow's `_DDL` constant
 * (`CREATE TABLE IF NOT EXISTS gold.repair_shadow_daily ...`), not by
 * any Laravel migration. RefreshDatabase never invokes the workflow,
 * so the table — and its broken policy — never appears in the test
 * DB. The legacy-GUC regression test
 * (`test_no_policy_references_legacy_georag_gucs`) is limited to
 * whatever pg_policies the test DB contains.
 *
 * **The fix.**
 *   1. This migration drops + re-creates the policy on the live PG
 *      cluster with the canonical workspace_isolation shape (USING
 *      only, fail-open on unset GUC, app.workspace_id).
 *   2. Sibling commit updates the Hatchet workflow's `_DDL` so fresh
 *      installs match (see repair_shadow_aggregate.py).
 *   3. The workflow itself also still calls
 *      `set_config('georag.workspace_id', …)` — that's part of a
 *      broader 13-file legacy-GUC writer gap tracked separately; for
 *      this table the workflow needs to be flipped to
 *      `app.workspace_id` in the same follow-up.
 *
 * SQLite (test DB) does not support RLS — gated on Postgres. The fix
 * is also gated on the table actually existing (it doesn't on a fresh
 * test DB), so this migration is a safe no-op until the Hatchet
 * workflow has run once.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        if (! $this->tableExists('gold', 'repair_shadow_daily')) {
            return;
        }

        DB::statement('ALTER TABLE gold.repair_shadow_daily ENABLE ROW LEVEL SECURITY');
        DB::statement('DROP POLICY IF EXISTS repair_shadow_daily_workspace_isolation ON gold.repair_shadow_daily');
        DB::statement(<<<'SQL'
            CREATE POLICY repair_shadow_daily_workspace_isolation ON gold.repair_shadow_daily
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        if (! $this->tableExists('gold', 'repair_shadow_daily')) {
            return;
        }

        // Drop the canonical policy only — do NOT recreate the broken
        // georag.* variant. Rolling back leaves the table RLS-on with
        // no policy (owner-sees-all under PG semantics); safer than
        // restoring a fail-closed bug.
        DB::statement('DROP POLICY IF EXISTS repair_shadow_daily_workspace_isolation ON gold.repair_shadow_daily');
    }

    private function tableExists(string $schema, string $table): bool
    {
        return DB::table('information_schema.tables')
            ->where('table_schema', $schema)
            ->where('table_name', $table)
            ->exists();
    }
};
