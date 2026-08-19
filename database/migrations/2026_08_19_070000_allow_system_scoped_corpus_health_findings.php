<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Drop NOT NULL from silver.corpus_health_findings.workspace_id so the
 * index-health agent can persist system-scoped findings.
 *
 * The problem
 * -----------
 * Every probe in `src/fastapi/app/agents/phase0/index_health.py` reads a
 * CLUSTER-scoped catalog — pg_stat_statements, pg_stat_user_tables,
 * pg_stat_user_indexes. None of them is per-tenant. The cron trigger
 * deliberately passes no workspace (`AgentRunInput.workspace_id`: "if None,
 * runs system-wide"), but the target column was
 * `NOT NULL REFERENCES silver.workspaces(workspace_id)`, so on its scheduled
 * run the agent could never persist a single finding: live logs showed
 * `null value in column "workspace_id" ... violates not-null constraint` on
 * every pass, swallowed by each probe's `except Exception`.
 *
 * The agent worked around this by collecting system-wide findings into its
 * summary and counting them as `findings_unpersisted` rather than writing
 * them — its own comment noted that "relaxing that NOT NULL is a tenancy
 * decision, not a bug fix, so it is NOT made here". This migration makes
 * that decision: system-scoped rows are legitimate and are represented by a
 * NULL workspace_id.
 *
 * Why no policy change is needed
 * ------------------------------
 * Unlike the tables in 2026_08_17_060000, this one does NOT need the
 * nullable three-clause policy retrofitted — it already has the equivalent.
 * Its phase0 policy (database/raw/phase0/95-rls-policies.sql, the
 * `tenant_isolation` macro) is written as
 *
 *     workspace_id IS NOT DISTINCT FROM NULLIF(current_setting(...), '')::uuid
 *     OR current_setting('app.workspace_id', true) IS NULL
 *     OR current_setting('app.workspace_id', true) = ''
 *
 * and `IS NOT DISTINCT FROM` is NULL-safe, so NULL rows are already visible
 * when the GUC is unset — which is exactly how the system-wide cron runs.
 * The WITH CHECK arm accepts the insert for the same reason. The only thing
 * that ever blocked a system-scoped row was the column constraint.
 *
 * The FK to silver.workspaces is deliberately kept: NULL satisfies a foreign
 * key, so a system-scoped row is still structurally valid while a row naming
 * a workspace is still forced to name a real one.
 *
 * Test DB
 * -------
 * silver.corpus_health_findings is created ONLY by
 * database/raw/phase0/70-layer-g-findings.sql and has never been transcribed
 * into a migrate-only test DB, so this is guarded on the table existing
 * rather than assumed present (same shape as the tableExists() guards in the
 * 2026_05_25 / 2026_08_14 RLS sweeps).
 */
return new class extends Migration
{
    private const QUALIFIED = 'silver.corpus_health_findings';

    public function up(): void
    {
        if (! $this->applies()) {
            return;
        }

        DB::statement('ALTER TABLE '.self::QUALIFIED.' ALTER COLUMN workspace_id DROP NOT NULL');
    }

    /**
     * Restoring NOT NULL requires removing the system-scoped rows this
     * migration exists to permit — there is no workspace to re-attribute
     * them to. That is the correct inverse (the rows could not have existed
     * before this migration ran), but it IS destructive, so it is stated
     * plainly here rather than hidden.
     */
    public function down(): void
    {
        if (! $this->applies()) {
            return;
        }

        DB::statement('DELETE FROM '.self::QUALIFIED.' WHERE workspace_id IS NULL');
        DB::statement('ALTER TABLE '.self::QUALIFIED.' ALTER COLUMN workspace_id SET NOT NULL');
    }

    /**
     * Postgres-only (SQLite fast suite has no RLS and no silver schema), and
     * only when the phase0 raw SQL that creates this table has actually run.
     */
    private function applies(): bool
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return false;
        }

        return (bool) DB::selectOne(
            'SELECT to_regclass(?) IS NOT NULL AS present',
            [self::QUALIFIED],
        )?->present;
    }
};
