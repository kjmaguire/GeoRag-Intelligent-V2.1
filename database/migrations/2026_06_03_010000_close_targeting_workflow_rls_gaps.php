<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Close RLS coverage gap on `targeting.*` and `workflow.*` workspace-
 * scoped tables that were missed by the Phase 0 raw-SQL block.
 *
 * Background — 2026-06-02 audit pass 5++ found
 * `targeting.target_recommendations` had `workspace_id NOT NULL` but no
 * RLS policy. The matching `PublicApiController::targets` endpoint
 * fetches rows by project_id with only Sanctum auth as the gate —
 * any authenticated tenant could fetch any other tenant's target
 * recommendations (the system's most sensitive output: ranked
 * drill-site recommendations + explanation markdown).
 *
 * Root cause: the Phase 0 raw-SQL RLS block (98-rls-tenant-isolation-block3)
 * enabled RLS on `target_backtests`, `target_score_factors`,
 * `target_uncertainties` but stopped short of `target_recommendations`
 * + `target_outcomes` + `target_review_decisions` + `target_scores` +
 * `target_candidate_zones`. The WorkspaceRlsCoverageTest didn't catch
 * the omission because its schema list excluded `targeting`.
 *
 * Same audit pass found `workflow.workflow_runs` + `workflow.workflow_run_events`
 * are workspace-scoped but lack RLS. They're admin-facing surfaces, so
 * less acute, but worth closing for consistency.
 *
 * Pattern matches the canonical `tenant_isolation` policy used elsewhere
 * (fail-open when GUC unset, matching workspace_id when set):
 *
 *   USING (
 *     NULLIF(current_setting('app.workspace_id', true), '') IS NULL
 *     OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
 *   )
 *
 * Audit reference: docs/handover/AUDIT_AND_FIX_REPORT.md — Theme H
 * (Pass 5++ extension: IDOR via target_recommendations).
 *
 * SQLite (test DB) does not support RLS — gated on Postgres.
 */
return new class extends Migration
{
    private const TABLES = [
        // targeting/* — write paths exist in app/Http/Controllers (target review),
        // read paths in PublicApiController + dashboards. All workspace-scoped.
        'targeting.target_recommendations',
        'targeting.target_outcomes',
        'targeting.target_review_decisions',
        'targeting.target_scores',
        'targeting.target_candidate_zones',
        // workflow/* — Hatchet run-level observability surfaces.
        // Partition children inherit the parent's policy automatically.
        'workflow.workflow_runs',
        'workflow.workflow_run_events',
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        foreach (self::TABLES as $tbl) {
            // Idempotent: ENABLE / FORCE are no-ops if already on.
            DB::statement("ALTER TABLE {$tbl} ENABLE ROW LEVEL SECURITY");
            DB::statement("ALTER TABLE {$tbl} FORCE ROW LEVEL SECURITY");

            $policy = str_replace('.', '_', $tbl).'_workspace_isolation';
            DB::statement("DROP POLICY IF EXISTS {$policy} ON {$tbl}");
            DB::statement(<<<SQL
                CREATE POLICY {$policy} ON {$tbl}
                  USING (
                    NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                    OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                  )
                  WITH CHECK (
                    NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                    OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                  )
            SQL);
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        foreach (self::TABLES as $tbl) {
            $policy = str_replace('.', '_', $tbl).'_workspace_isolation';
            DB::statement("DROP POLICY IF EXISTS {$policy} ON {$tbl}");
            // Intentionally do NOT disable RLS on rollback — leaving RLS
            // enabled with no policy is safer (fails closed) than
            // disabling it and silently opening cross-tenant reads.
        }
    }
};
