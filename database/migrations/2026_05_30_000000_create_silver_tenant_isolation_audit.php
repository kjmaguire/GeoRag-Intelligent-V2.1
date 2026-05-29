<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Z-roadmap Z.9 — `graph_tenant_audit` nightly verifier.
 *
 * Persisted run log for the Phase 0 tenant-isolation auditors (both the
 * existing Postgres RLS auditor and the new Neo4j graph_tenant_auditor).
 * Each row is one nightly run, summarising violations across stores so
 * the ops dashboard can chart isolation health over time without
 * scanning the per-row `silver.store_reconciliation_findings` table.
 *
 * Schema mirrors the agent return shape:
 *   - pg_violations    — sum of Postgres RLS probe violations
 *   - graph_violations — sum of Neo4j cross-workspace edge / orphan / missing
 *     workspace_id violations (new column for Z.9; see
 *     graph_tenant_auditor.py)
 *   - violation_details — JSONB blob of the full per-store breakdown
 *
 * RLS is intentionally NOT enabled — this is a platform-wide audit log
 * (workspace_id is nullable because system-wide sweeps have no scope),
 * gated by the Laravel admin Gate per the same pattern as
 * `workflow.flow_jwt_keys`.
 *
 * Per memory project_pg_role_membership_gap_2026_05_22.md, NOT applied
 * by this commit — Kyle runs via
 *   php artisan migrate --database=pgsql_migrations
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.tenant_isolation_audit (
                audit_run_id        UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id        UUID         NULL,
                auditor             TEXT         NOT NULL,
                pg_violations       INTEGER      NOT NULL DEFAULT 0,
                graph_violations    INTEGER      NOT NULL DEFAULT 0,
                tables_probed       INTEGER      NOT NULL DEFAULT 0,
                edges_probed        BIGINT       NOT NULL DEFAULT 0,
                nodes_probed        BIGINT       NOT NULL DEFAULT 0,
                violation_details   JSONB        NOT NULL DEFAULT '{}'::jsonb,
                started_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                finished_at         TIMESTAMPTZ  NULL,
                CONSTRAINT tenant_isolation_audit_pkey
                    PRIMARY KEY (audit_run_id),
                CONSTRAINT tenant_isolation_audit_auditor_valid
                    CHECK (auditor IN ('postgres_rls', 'neo4j_graph', 'combined'))
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS idx_tenant_isolation_audit_started
                       ON silver.tenant_isolation_audit (started_at DESC)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_tenant_isolation_audit_auditor_started
                       ON silver.tenant_isolation_audit (auditor, started_at DESC)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_tenant_isolation_audit_open_violations
                       ON silver.tenant_isolation_audit (started_at DESC)
                       WHERE pg_violations > 0 OR graph_violations > 0');

        DB::statement('COMMENT ON TABLE silver.tenant_isolation_audit IS '
            ."'Z-roadmap Z.9 — per-run audit log for tenant_isolation_audit "
            ."(Postgres RLS) and graph_tenant_audit (Neo4j) verifiers. "
            ."Aggregated counters; per-row findings live in "
            ."silver.store_reconciliation_findings.'");

        DB::statement('GRANT USAGE ON SCHEMA silver TO georag_app');
        DB::statement('GRANT SELECT, INSERT, UPDATE
                       ON silver.tenant_isolation_audit TO georag_app');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS silver.tenant_isolation_audit CASCADE');
    }
};
