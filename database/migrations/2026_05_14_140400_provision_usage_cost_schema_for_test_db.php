<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Sibling to 2026_05_14_140000 / 140100 / 140200 / 140300 — provision the
 * `usage` schema + cost tables in the Laravel test DB.
 *
 * Production gets these from raw SQL (database/raw/phase0/60-layer-f-
 * usage-cost.sql). Later Laravel migration 2026_05_20_050000_create_
 * workspace_cost_quotas.php ALTERs all three tables (adds columns +
 * enables RLS), so the test DB needs minimal mirrors.
 *
 * Tables:
 *   - usage.usage_events — non-partitioned in test (raw version is
 *     monthly-partitioned via pg_partman; tests don't exercise
 *     partition management).
 *   - usage.usage_aggregates_daily — composite-PK rollup, mirror as-is.
 *   - usage.workspace_cost_ceilings — base shape WITHOUT suspended_at /
 *     suspended_reason; the 2026_05_20_050000 ADD COLUMN IF NOT EXISTS
 *     adds those on top.
 *
 * `CREATE TABLE IF NOT EXISTS` is a no-op on production where the raw SQL
 * already ran.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('CREATE SCHEMA IF NOT EXISTS usage');

        // ─────────────────────── usage.usage_events ─────────────────────
        // Non-partitioned mirror. PK is (id, created_at) to match raw shape.
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS usage.usage_events (
                id                      BIGSERIAL    NOT NULL,
                workspace_id            UUID         NULL,
                agent_name              TEXT         NOT NULL,
                agent_version           TEXT         NULL,
                model_profile           TEXT         NOT NULL,
                model_id                TEXT         NULL,
                tokens_prompt           INTEGER      NOT NULL DEFAULT 0,
                tokens_completion       INTEGER      NOT NULL DEFAULT 0,
                tokens_total            INTEGER      GENERATED ALWAYS AS (tokens_prompt + tokens_completion) STORED,
                projected_cost_usd      NUMERIC(12, 6) NOT NULL DEFAULT 0,
                latency_ms              INTEGER      NULL,
                outcome                 TEXT         NOT NULL DEFAULT 'success'
                    CHECK (outcome IN ('success','refusal','failure','timeout','circuit_open')),
                trace_id                TEXT         NULL,
                invocation_id           UUID         NULL,
                parent_workflow_run_id  UUID         NULL,
                created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT usage_events_test_pkey PRIMARY KEY (id, created_at)
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS usage_events_workspace_idx
                       ON usage.usage_events (workspace_id, created_at DESC)');
        DB::statement('CREATE INDEX IF NOT EXISTS usage_events_agent_idx
                       ON usage.usage_events (agent_name, created_at DESC)');
        DB::statement('CREATE INDEX IF NOT EXISTS usage_events_trace_id_idx
                       ON usage.usage_events (trace_id) WHERE trace_id IS NOT NULL');

        // ────────────────────── usage.usage_aggregates_daily ────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS usage.usage_aggregates_daily (
                workspace_id            UUID         NOT NULL,
                agent_name              TEXT         NOT NULL,
                model_profile           TEXT         NOT NULL,
                rollup_date             DATE         NOT NULL,
                invocations_total       BIGINT       NOT NULL DEFAULT 0,
                invocations_success     BIGINT       NOT NULL DEFAULT 0,
                invocations_failure     BIGINT       NOT NULL DEFAULT 0,
                tokens_prompt_total     BIGINT       NOT NULL DEFAULT 0,
                tokens_completion_total BIGINT       NOT NULL DEFAULT 0,
                cost_usd_total          NUMERIC(14, 6) NOT NULL DEFAULT 0,
                last_updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                PRIMARY KEY (workspace_id, agent_name, model_profile, rollup_date)
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS usage_aggregates_daily_workspace_date_idx
                       ON usage.usage_aggregates_daily (workspace_id, rollup_date DESC)');

        // ────────────────────── usage.workspace_cost_ceilings ───────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS usage.workspace_cost_ceilings (
                workspace_id                UUID         PRIMARY KEY
                    REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                monthly_ceiling_usd         NUMERIC(12, 2) NOT NULL,
                soft_warn_threshold_pct     SMALLINT     NOT NULL DEFAULT 80
                    CHECK (soft_warn_threshold_pct BETWEEN 1 AND 100),
                hard_stop_threshold_pct     SMALLINT     NOT NULL DEFAULT 100
                    CHECK (hard_stop_threshold_pct BETWEEN 1 AND 200),
                admin_override_enabled      BOOLEAN      NOT NULL DEFAULT FALSE,
                admin_override_expires_at   TIMESTAMPTZ  NULL,
                last_warn_sent_at           TIMESTAMPTZ  NULL,
                last_warn_pct               SMALLINT     NULL,
                updated_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_by                  BIGINT       NULL,
                CONSTRAINT workspace_cost_ceilings_thresholds_ordered
                    CHECK (soft_warn_threshold_pct <= hard_stop_threshold_pct)
            )
        SQL);

        DB::statement('GRANT USAGE ON SCHEMA usage TO georag_app');
        DB::statement('GRANT SELECT, INSERT, UPDATE ON usage.usage_events TO georag_app');
        DB::statement('GRANT SELECT, INSERT, UPDATE ON usage.usage_aggregates_daily TO georag_app');
        DB::statement('GRANT SELECT, INSERT, UPDATE ON usage.workspace_cost_ceilings TO georag_app');
        DB::statement('GRANT USAGE ON ALL SEQUENCES IN SCHEMA usage TO georag_app');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // Gate the usage_events drop by relkind — on prod the partitioned
        // parent owns the table and DROP would error.
        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'usage' AND c.relname = 'usage_events'
                      AND c.relkind = 'r'
                ) THEN
                    DROP TABLE IF EXISTS usage.usage_events CASCADE;
                END IF;
            END $$;
        SQL);
        DB::statement('DROP TABLE IF EXISTS usage.usage_aggregates_daily CASCADE');
        DB::statement('DROP TABLE IF EXISTS usage.workspace_cost_ceilings CASCADE');
    }
};
