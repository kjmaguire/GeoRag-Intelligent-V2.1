<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Doc-phase 174 — audit.* test-DB provisioning.
 *
 * Production DBs get audit.audit_ledger from raw SQL apply
 * (database/raw/phase0/20-layer-b-audit-ledger.sql + 90-audit-hash-
 * chain-trigger.sql). That raw SQL needs:
 *   - pg_partman extension
 *   - manually-pre-created partitions
 *   - hash-chain trigger function
 *
 * For the Laravel test DB (`georag_test`), the dashboard controllers
 * just need a non-partitioned audit.audit_ledger that responds to
 * SELECT — tests don't exercise partition management or the hash
 * trigger. So this migration creates a minimal mirror.
 *
 * `CREATE TABLE IF NOT EXISTS` is a no-op on production where the
 * partitioned parent already exists; on the test DB the simple table
 * is created fresh. Same column shape so the dashboard SELECTs work
 * either way.
 *
 * Closes 3 of 14 Track3DashboardsTest failures:
 *   - test_decision_history_admin_renders_with_expected_props
 *   - test_support_cockpit_admin_renders_with_expected_props
 *   - test_support_cockpit_status_filter_passes_through
 *
 * Each fails today with "relation audit.audit_ledger does not exist"
 * because RefreshDatabase + Laravel migrations don't create it.
 */
return new class extends Migration
{
    public function up(): void
    {
        // sqlite tests skip cleanly
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('CREATE SCHEMA IF NOT EXISTS audit');

        // ─────────────────────── audit.audit_ledger ────────────────────
        // Minimal non-partitioned mirror. Matches the column shape of
        // the production partitioned table so dashboard SELECT queries
        // see consistent results. No hash trigger — test fixtures
        // insert rows directly when needed.
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS audit.audit_ledger (
                id              UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id    UUID         NULL,
                actor_id        BIGINT       NULL,
                actor_kind      TEXT         NOT NULL DEFAULT 'user'
                    CHECK (actor_kind IN ('user', 'system', 'agent', 'workflow', 'external')),
                action_type     TEXT         NOT NULL,
                target_schema   TEXT         NULL,
                target_table    TEXT         NULL,
                target_id       TEXT         NULL,
                payload         JSONB        NOT NULL DEFAULT '{}'::jsonb,
                previous_hash   BYTEA        NULL,
                hash            BYTEA        NULL,
                trace_id        TEXT         NULL,
                created_at      TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp(),
                CONSTRAINT audit_ledger_test_pkey PRIMARY KEY (id, created_at)
            )
        SQL);

        // Indexes match production for query-plan parity.
        DB::statement('CREATE INDEX IF NOT EXISTS audit_ledger_workspace_id_idx
                       ON audit.audit_ledger (workspace_id, created_at DESC)');
        DB::statement('CREATE INDEX IF NOT EXISTS audit_ledger_action_type_idx
                       ON audit.audit_ledger (action_type, created_at DESC)');
        DB::statement('CREATE INDEX IF NOT EXISTS audit_ledger_target_idx
                       ON audit.audit_ledger (target_schema, target_table, target_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS audit_ledger_trace_id_idx
                       ON audit.audit_ledger (trace_id) WHERE trace_id IS NOT NULL');

        // ─────────── audit.audit_ledger_verification_runs ──────────────
        // Read by the verifier nightly + the dashboard. Same minimal
        // shape; test fixtures don't need to exercise verification.
        // `workspace_id` matches production (nullable per the later
        // 2026_05_17 migration) so subsequent ALTERs run cleanly here.
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS audit.audit_ledger_verification_runs (
                id              UUID         NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
                workspace_id    UUID         NULL,
                partition_date  DATE         NOT NULL,
                status          TEXT         NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'clean', 'break', 'error')),
                rows_verified   BIGINT       NOT NULL DEFAULT 0,
                started_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                completed_at    TIMESTAMPTZ  NULL,
                error_text      TEXT         NULL
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS audit_ledger_verification_runs_partition_date_idx
                       ON audit.audit_ledger_verification_runs (partition_date DESC)');
        DB::statement('CREATE INDEX IF NOT EXISTS audit_ledger_verification_runs_status_idx
                       ON audit.audit_ledger_verification_runs (status, started_at DESC)');

        // ─────────────────────────── Grants ────────────────────────────
        DB::statement('GRANT USAGE ON SCHEMA audit TO georag_app');
        DB::statement('GRANT SELECT, INSERT ON audit.audit_ledger TO georag_app');
        DB::statement('GRANT SELECT, INSERT, UPDATE ON audit.audit_ledger_verification_runs TO georag_app');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // Down only drops the test-DB shape — on production where the
        // partitioned parent owns the table, this would error and we
        // don't want that. Gate by row-existence-check.
        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'audit' AND c.relname = 'audit_ledger'
                      AND c.relkind = 'r'  -- 'r' = ordinary table, 'p' = partitioned
                ) THEN
                    DROP TABLE IF EXISTS audit.audit_ledger CASCADE;
                END IF;
            END $$;
        SQL);
        DB::statement('DROP TABLE IF EXISTS audit.audit_ledger_verification_runs CASCADE');
    }
};
