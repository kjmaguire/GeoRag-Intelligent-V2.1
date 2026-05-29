<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * SECURITY FIX 2026-05-28 — close the chr(0) sentinel bug on the two
 * silver RLS policies missed by the 2026-05-25 broken-GUC sweep.
 *
 * **The bug.** Both `silver.workspaces.workspaces_tenant_isolation` and
 * `silver.target_rationales.target_rationales_workspace_isolation` use
 * `NULLIF(current_setting('app.workspace_id', true), chr(0))` as the
 * "GUC unset → fail open" sentinel. `chr(0)` produces a TEXT containing
 * a U+0000 byte, which PostgreSQL 18 rejects (`null character not
 * permitted`) because text columns cannot store NUL. The policy was
 * meant to fail OPEN when the GUC is unset (all rows visible), but
 * under psycopg2 the policy evaluation itself fails CLOSED:
 *
 *     ERROR:  null character not permitted
 *
 * Discovered 2026-05-28 during TIER 0e reranker label mining, when
 * `_mine_reranker_labels_from_answer_runs.py` connected as `georag_app`
 * (the RLS-bound runtime role) and could not SELECT from either table.
 *
 * **Why these two slipped through 2026_05_25_180924.** That migration
 * only swept policies referencing the legacy `georag.workspace_id` GUC.
 * These two already use the canonical `app.workspace_id` — they just
 * use the wrong empty-value sentinel (chr(0) instead of ''). The
 * `test_no_policy_references_legacy_georag_gucs` regression test
 * couldn't catch them for the same reason.
 *
 * **The fix.** Replace each broken policy with the same shape but with
 * the intended empty-string sentinel (`''`), mirroring the canonical
 * policies installed by 2026_05_25_180924 + 2026_05_25_182857.
 *
 *   silver.workspaces           — direct workspace_id self-check
 *   silver.target_rationales    — EXISTS join through
 *                                 targeting.target_recommendations
 *                                 (target_rationales has no
 *                                 workspace_id column of its own)
 *
 * The workaround in `scripts/_mine_reranker_labels_from_answer_runs.py`
 * (connecting as POSTGRES_OWNER_USER) stays in place after this fix
 * as belt-and-suspenders for the maintenance role.
 *
 * SQLite (test DB) does not support RLS — gated on Postgres. The
 * target_rationales policy is also gated on `targeting.target_recommendations`
 * existing, which it may not on a fresh test DB.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        $this->fixWorkspacesPolicy();
        $this->fixTargetRationalesPolicy();
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // Best-effort reversal — drops the canonical policies this
        // migration installed. We do NOT re-create the broken chr(0)
        // versions; rolling back leaves the tables RLS-on with no
        // policy, which under PG semantics means owners see all rows
        // and non-owners see none. Safer than restoring a fail-closed
        // bug.
        if ($this->tableExists('silver', 'workspaces')) {
            DB::statement('DROP POLICY IF EXISTS workspaces_tenant_isolation ON silver.workspaces');
        }
        if ($this->tableExists('silver', 'target_rationales')) {
            DB::statement('DROP POLICY IF EXISTS target_rationales_workspace_isolation ON silver.target_rationales');
        }
    }

    private function fixWorkspacesPolicy(): void
    {
        if (! $this->tableExists('silver', 'workspaces')) {
            return;
        }

        DB::statement('ALTER TABLE silver.workspaces ENABLE ROW LEVEL SECURITY');
        DB::statement('DROP POLICY IF EXISTS workspaces_tenant_isolation ON silver.workspaces');
        DB::statement(<<<'SQL'
            CREATE POLICY workspaces_tenant_isolation ON silver.workspaces
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);
    }

    private function fixTargetRationalesPolicy(): void
    {
        if (! $this->tableExists('silver', 'target_rationales')) {
            return;
        }
        // The policy references targeting.target_recommendations in its
        // EXISTS join. If that table is missing (e.g. fresh test DB
        // without the targeting schema bootstrap), skip the install so
        // CREATE POLICY doesn't fail; the catalog stays clean and the
        // table remains owner-only-visible until targeting lands.
        if (! $this->tableExists('targeting', 'target_recommendations')) {
            DB::statement('DROP POLICY IF EXISTS target_rationales_workspace_isolation ON silver.target_rationales');

            return;
        }

        DB::statement('ALTER TABLE silver.target_rationales ENABLE ROW LEVEL SECURITY');
        DB::statement('DROP POLICY IF EXISTS target_rationales_workspace_isolation ON silver.target_rationales');
        DB::statement(<<<'SQL'
            CREATE POLICY target_rationales_workspace_isolation ON silver.target_rationales
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR EXISTS (
                  SELECT 1
                    FROM targeting.target_recommendations r
                   WHERE r.recommendation_id = target_rationales.recommendation_id
                     AND r.workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                )
              )
        SQL);
    }

    private function tableExists(string $schema, string $table): bool
    {
        return DB::table('information_schema.tables')
            ->where('table_schema', $schema)
            ->where('table_name', $table)
            ->exists();
    }
};
