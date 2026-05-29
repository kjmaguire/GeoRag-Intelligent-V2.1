<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Cost ceiling §35.1 — hard-stop enforcement on top of existing
 * Phase-0 schema.
 *
 * The Phase-0 raw SQL (database/raw/phase0/60-layer-f-usage-cost.sql)
 * already created:
 *   - usage.usage_events
 *   - usage.usage_aggregates_daily
 *   - usage.workspace_cost_ceilings
 *
 * That gave us soft-warn alerting via the existing
 * `cost_burn_watcher` Hatchet workflow. This migration adds the
 * remaining piece — hard-stop suspension state — so the orchestrator
 * can refuse new LLM calls when accrued >= ceiling.
 *
 * Why a separate migration (not patching the raw SQL)
 * ---------------------------------------------------
 * The raw SQL files run from a fresh init; live dev/prod databases
 * have already executed them. A Laravel migration applies
 * incrementally on top of the live schema without rerunning the
 * partition setup or rewriting the existing tables.
 *
 * What ships here
 * ---------------
 *   1. ADD COLUMN workspace_cost_ceilings.suspended_at (timestamptz)
 *      — set by cost_burn_watcher when accrued >= hard_stop threshold
 *      and admin_override_enabled is false. NULL = active.
 *
 *   2. ADD COLUMN workspace_cost_ceilings.suspended_reason (text)
 *      — human-readable explanation surfaced in the 429 response and
 *      the operator dashboard.
 *
 *   3. RLS policies on workspace_cost_ceilings + usage_events (the
 *      Phase-0 raw SQL didn't gate these — Eval 12 P1 follow-up).
 *
 * SQLite (test DB) — gated on Postgres.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE usage.workspace_cost_ceilings
              ADD COLUMN IF NOT EXISTS suspended_at timestamptz,
              ADD COLUMN IF NOT EXISTS suspended_reason text
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN usage.workspace_cost_ceilings.suspended_at IS
              'Set by cost_burn_watcher when accrued spend reaches hard_stop_threshold_pct of monthly_ceiling_usd AND admin_override_enabled = false. The pre-LLM-call check in app.agent.llm_calls reads a Redis cache of this state and raises WorkspaceQuotaExceeded → HTTP 429. Cleared at calendar-month rollover or via admin route.'
        SQL);

        // RLS on cost tables — Eval 12 follow-up.
        // DROP POLICY IF EXISTS guards added 2026-05-22 so RefreshDatabase
        // test runs don't trip "policy already exists" when migrate:fresh
        // leaves residual policies on non-default schemas.
        DB::statement('ALTER TABLE usage.workspace_cost_ceilings ENABLE ROW LEVEL SECURITY');
        DB::statement('DROP POLICY IF EXISTS workspace_cost_ceilings_tenant_isolation ON usage.workspace_cost_ceilings');
        DB::statement(<<<'SQL'
            CREATE POLICY workspace_cost_ceilings_tenant_isolation ON usage.workspace_cost_ceilings
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);

        DB::statement('ALTER TABLE usage.usage_events ENABLE ROW LEVEL SECURITY');
        DB::statement('DROP POLICY IF EXISTS usage_events_tenant_isolation ON usage.usage_events');
        DB::statement(<<<'SQL'
            CREATE POLICY usage_events_tenant_isolation ON usage.usage_events
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);

        DB::statement('ALTER TABLE usage.usage_aggregates_daily ENABLE ROW LEVEL SECURITY');
        DB::statement('DROP POLICY IF EXISTS usage_aggregates_daily_tenant_isolation ON usage.usage_aggregates_daily');
        DB::statement(<<<'SQL'
            CREATE POLICY usage_aggregates_daily_tenant_isolation ON usage.usage_aggregates_daily
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

        DB::statement('DROP POLICY IF EXISTS usage_aggregates_daily_tenant_isolation ON usage.usage_aggregates_daily');
        DB::statement('DROP POLICY IF EXISTS usage_events_tenant_isolation ON usage.usage_events');
        DB::statement('DROP POLICY IF EXISTS workspace_cost_ceilings_tenant_isolation ON usage.workspace_cost_ceilings');

        DB::statement('ALTER TABLE usage.usage_aggregates_daily DISABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE usage.usage_events DISABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE usage.workspace_cost_ceilings DISABLE ROW LEVEL SECURITY');

        DB::statement(<<<'SQL'
            ALTER TABLE usage.workspace_cost_ceilings
              DROP COLUMN IF EXISTS suspended_at,
              DROP COLUMN IF EXISTS suspended_reason
        SQL);
    }
};
