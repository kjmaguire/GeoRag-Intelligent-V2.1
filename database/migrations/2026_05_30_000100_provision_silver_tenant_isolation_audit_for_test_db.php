<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Test-DB parity sibling for
 *   2026_05_30_000000_create_silver_tenant_isolation_audit.
 *
 * Per memory project_test_db_parity_gap.md, raw-SQL silver tables need
 * an explicit test-DB sibling so the integration suite (which uses the
 * georag_test owner role) can write to the table without the production
 * GRANT-to-georag_app step. Schema is identical to production; this
 * migration is idempotent.
 *
 * NOT applied by this commit.
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
    }

    public function down(): void
    {
        // Production migration handles drop; this sibling is intentionally
        // non-destructive on rollback.
    }
};
