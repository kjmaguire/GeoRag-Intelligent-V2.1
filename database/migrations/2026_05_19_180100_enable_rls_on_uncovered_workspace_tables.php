<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Close RLS coverage gaps on workspace-scoped tables that were created
 * after the Phase 0 RLS block (database/raw/phase0/100-rls-tenant-*)
 * and never had ENABLE ROW LEVEL SECURITY applied.
 *
 * Pattern matches the canonical `tenant_isolation` policy used elsewhere
 * (see `audit.audit_ledger`, `silver.agent_conversations`, etc.):
 *
 *   USING (
 *     NULLIF(current_setting('app.workspace_id', true), '') IS NULL
 *     OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
 *   )
 *
 * Fail-open semantics when the GUC is unset — enforcement is the
 * application's responsibility (every per-request transaction must set
 * `SET LOCAL app.workspace_id = '<uuid>'`). This migration only fixes
 * the missing-RLS gap; the GUC contract is already enforced upstream
 * in `src/fastapi/app/deps.py`.
 *
 * Tables covered:
 *   1. silver.collab_anchors          (has workspace_id)
 *   2. silver.collab_comments         (workspace_id reachable via anchor_id)
 *   3. silver.tier3_unlock_requests   (has workspace_id)
 *   4. audit.query_audit_log          (has workspace_id; nullable for
 *                                      system-level audit rows, matches
 *                                      the audit_ledger policy shape)
 *   5. silver.qp_credentials          (user-scoped, not workspace-scoped —
 *                                      uses `app.current_user_id` GUC
 *                                      with the same fail-open shape;
 *                                      enforcement still pending in
 *                                      service layer, but enabling RLS
 *                                      now means tightening the policy
 *                                      later is a one-line change rather
 *                                      than a multi-table change)
 *
 * NOT covered (intentionally — documented gaps tracked separately):
 *   - silver.workspaces — needs membership-based policy (joins
 *     workspace_memberships), not a tenant filter.
 *   - silver.target_rationales — workspace_id is reachable only via
 *     recommendation_id → target_recommendations; needs a subquery
 *     policy. Defer until recommendation table contract is locked.
 *   - gold.h3_density_mineral, silver.geological_ontology_terms,
 *     silver.geological_ontology_synonyms — cross-workspace reference
 *     and aggregate data; RLS would be the wrong pattern.
 *
 * SQLite (test DB) does not support RLS — gated on Postgres.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // 1–3: Direct workspace_id tables.
        foreach ([
            'silver.collab_anchors',
            'silver.tier3_unlock_requests',
        ] as $tbl) {
            DB::statement("ALTER TABLE {$tbl} ENABLE ROW LEVEL SECURITY");
            $policy = str_replace('.', '_', $tbl).'_workspace_isolation';
            DB::statement(<<<SQL
                CREATE POLICY {$policy} ON {$tbl}
                  USING (
                    NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                    OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                  )
            SQL);
        }

        // 4: audit.query_audit_log — allow workspace_id IS NULL (system rows).
        DB::statement('ALTER TABLE audit.query_audit_log ENABLE ROW LEVEL SECURITY');
        DB::statement(<<<'SQL'
            CREATE POLICY query_audit_log_workspace_isolation ON audit.query_audit_log
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);

        // 2 (deferred above — needs anchor join):
        // silver.collab_comments — workspace_id is on the parent anchor.
        DB::statement('ALTER TABLE silver.collab_comments ENABLE ROW LEVEL SECURITY');
        DB::statement(<<<'SQL'
            CREATE POLICY collab_comments_workspace_isolation ON silver.collab_comments
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR EXISTS (
                  SELECT 1 FROM silver.collab_anchors a
                  WHERE a.anchor_id = silver.collab_comments.anchor_id
                    AND a.workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                )
              )
        SQL);

        // 5: silver.qp_credentials — user-scoped. Uses a different GUC
        // (`app.current_user_id`). Currently no caller sets this GUC, so
        // the fail-open shape means existing reads continue to work
        // unchanged. Hardening the service layer to SET this GUC is a
        // follow-up; turning RLS on now means that hardening becomes a
        // one-line policy tightening, not a multi-table audit.
        DB::statement('ALTER TABLE silver.qp_credentials ENABLE ROW LEVEL SECURITY');
        DB::statement(<<<'SQL'
            CREATE POLICY qp_credentials_user_isolation ON silver.qp_credentials
              USING (
                NULLIF(current_setting('app.current_user_id', true), '') IS NULL
                OR user_id::text = NULLIF(current_setting('app.current_user_id', true), '')
              )
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        foreach ([
            ['silver.collab_anchors', 'silver_collab_anchors_workspace_isolation'],
            ['silver.collab_comments', 'collab_comments_workspace_isolation'],
            ['silver.tier3_unlock_requests', 'silver_tier3_unlock_requests_workspace_isolation'],
            ['audit.query_audit_log', 'query_audit_log_workspace_isolation'],
            ['silver.qp_credentials', 'qp_credentials_user_isolation'],
        ] as [$tbl, $policy]) {
            DB::statement("DROP POLICY IF EXISTS {$policy} ON {$tbl}");
            DB::statement("ALTER TABLE {$tbl} DISABLE ROW LEVEL SECURITY");
        }
    }
};
