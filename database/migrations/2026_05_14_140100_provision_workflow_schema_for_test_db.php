<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Sibling to 2026_05_14_140000 — provision a minimal workflow.* mirror in
 * the Laravel test DB (`georag_test`).
 *
 * Production DBs get workflow.workflow_runs + workflow_run_events from raw
 * SQL apply (database/raw/phase0/30-layer-c-workflow-runs.sql), which needs
 * pg_partman and creates a partitioned parent. The Laravel test DB does
 * not apply phase0 raw SQL, so subsequent Laravel migrations that ALTER /
 * UPDATE these tables (e.g. 2026_05_17_120000_drop_workflow_activepieces_
 * channels, 2026_05_19_180000_drop_activepieces_from_workflow_engine_check)
 * crashed migrate:fresh with "relation workflow.workflow_runs does not
 * exist".
 *
 * `CREATE TABLE IF NOT EXISTS` is a no-op on production where the
 * partitioned parent already exists; on the test DB the simple table is
 * created fresh. Same column shape so the downstream ALTERs land cleanly
 * either way.
 *
 * Engine CHECK matches the original phase0 vocabulary (including
 * 'activepieces') so the 2026_05_19_180000 migration's DROP+ADD CONSTRAINT
 * has the same starting state in both environments.
 */
return new class extends Migration
{
    public function up(): void
    {
        // sqlite tests skip cleanly
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('CREATE SCHEMA IF NOT EXISTS workflow');

        // ─────────────────────── workflow.workflow_runs ─────────────────
        // Minimal non-partitioned mirror. Matches the column shape of the
        // production partitioned parent so SELECT/UPDATE queries behave
        // identically. No pg_partman wiring — tests don't exercise
        // partition management.
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS workflow.workflow_runs (
                run_id          UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id    UUID         NULL,
                workflow_kind   TEXT         NOT NULL,
                engine          TEXT         NOT NULL
                    CHECK (engine IN ('hatchet','activepieces','langgraph','dagster','horizon','reverb')),
                engine_run_id   TEXT         NULL,
                status          TEXT         NOT NULL
                    CHECK (status IN ('queued','running','success','failure','cancelled','timed_out')),
                trace_id        TEXT         NULL,
                started_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                ended_at        TIMESTAMPTZ  NULL,
                duration_ms     BIGINT       GENERATED ALWAYS AS (
                                    CASE WHEN ended_at IS NULL THEN NULL
                                         ELSE EXTRACT(EPOCH FROM (ended_at - started_at)) * 1000 END
                                ) STORED,
                input_summary   JSONB        NOT NULL DEFAULT '{}'::jsonb,
                output_summary  JSONB        NOT NULL DEFAULT '{}'::jsonb,
                failure_reason  JSONB        NULL,
                triggered_by    BIGINT       NULL,
                CONSTRAINT workflow_runs_test_pkey PRIMARY KEY (run_id, started_at)
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS workflow_runs_workspace_id_idx
                       ON workflow.workflow_runs (workspace_id, started_at DESC)');
        DB::statement('CREATE INDEX IF NOT EXISTS workflow_runs_kind_status_idx
                       ON workflow.workflow_runs (workflow_kind, status, started_at DESC)');
        DB::statement('CREATE INDEX IF NOT EXISTS workflow_runs_engine_run_id_idx
                       ON workflow.workflow_runs (engine, engine_run_id) WHERE engine_run_id IS NOT NULL');
        DB::statement('CREATE INDEX IF NOT EXISTS workflow_runs_trace_id_idx
                       ON workflow.workflow_runs (trace_id) WHERE trace_id IS NOT NULL');

        // ─────────────────── workflow.workflow_run_events ───────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS workflow.workflow_run_events (
                id              BIGSERIAL    PRIMARY KEY,
                run_id          UUID         NOT NULL,
                workspace_id    UUID         NULL,
                step_name       TEXT         NULL,
                event_type      TEXT         NOT NULL,
                payload         JSONB        NOT NULL DEFAULT '{}'::jsonb,
                occurred_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS workflow_run_events_run_id_idx
                       ON workflow.workflow_run_events (run_id, occurred_at)');
        DB::statement('CREATE INDEX IF NOT EXISTS workflow_run_events_workspace_id_idx
                       ON workflow.workflow_run_events (workspace_id, occurred_at DESC) WHERE workspace_id IS NOT NULL');
        DB::statement('CREATE INDEX IF NOT EXISTS workflow_run_events_event_type_idx
                       ON workflow.workflow_run_events (event_type, occurred_at DESC)');

        // ─────────────────────────── Grants ─────────────────────────────
        DB::statement('GRANT USAGE ON SCHEMA workflow TO georag_app');
        DB::statement('GRANT SELECT, INSERT, UPDATE ON workflow.workflow_runs TO georag_app');
        DB::statement('GRANT SELECT, INSERT ON workflow.workflow_run_events TO georag_app');
        DB::statement('GRANT USAGE ON ALL SEQUENCES IN SCHEMA workflow TO georag_app');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // Down only drops the test-DB shape — on production where a
        // partitioned parent owns the table, this would error and we
        // don't want that. Gate by relkind row-existence-check.
        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'workflow' AND c.relname = 'workflow_runs'
                      AND c.relkind = 'r'  -- 'r' = ordinary table, 'p' = partitioned
                ) THEN
                    DROP TABLE IF EXISTS workflow.workflow_runs CASCADE;
                END IF;
            END $$;
        SQL);
        DB::statement('DROP TABLE IF EXISTS workflow.workflow_run_events CASCADE');
    }
};
