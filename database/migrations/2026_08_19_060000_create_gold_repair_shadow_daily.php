<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Declares gold.repair_shadow_daily, which until now was created at runtime by
 * the repair_shadow_aggregate Hatchet workflow.
 *
 * That workflow opened each run with `CREATE SCHEMA IF NOT EXISTS gold` +
 * `CREATE TABLE IF NOT EXISTS ...` as `georag_app`. Creating a schema requires
 * CREATE on the database, which the application role does not have on Azure —
 * correctly, it is a low-privilege role — so every scheduled run died with:
 *
 *   asyncpg.exceptions.InsufficientPrivilegeError: permission denied for database georag
 *
 * The table therefore never existed in production and the aggregate has never
 * been written. Locally it worked only because the dev role is broader, which
 * is exactly how the divergence went unnoticed.
 *
 * Runtime DDL is the defect, not the missing privilege: schema belongs in the
 * migration chain where it is reviewed, ordered and applied once. The workflow
 * loses its _DDL block in the same commit and now assumes the table exists.
 *
 * The RLS policy is reproduced verbatim from the workflow's _DDL, including its
 * fail-CLOSED shape — with app.workspace_id unset, current_setting returns NULL,
 * the comparison is NULL, and no rows are visible. That is the safe direction
 * and deliberately not "normalised" to the fail-open form used elsewhere.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('CREATE SCHEMA IF NOT EXISTS gold');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS gold.repair_shadow_daily (
                workspace_id            UUID         NOT NULL,
                for_date                DATE         NOT NULL,
                total_queries           INTEGER      NOT NULL DEFAULT 0,
                guard_pass_count        INTEGER      NOT NULL DEFAULT 0,
                queries_with_failures   INTEGER      NOT NULL DEFAULT 0,
                top_guard_codes         JSONB        NOT NULL DEFAULT '{}'::jsonb,
                top_repair_strategies   JSONB        NOT NULL DEFAULT '{}'::jsonb,
                evidence_kind_counts    JSONB        NOT NULL DEFAULT '{}'::jsonb,
                budget_pressure_buckets JSONB        NOT NULL DEFAULT '{}'::jsonb,
                avg_latency_ms          INTEGER      NULL,
                p95_latency_ms          INTEGER      NULL,
                created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                PRIMARY KEY (workspace_id, for_date)
            )
        SQL);

        DB::statement('ALTER TABLE gold.repair_shadow_daily ENABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE gold.repair_shadow_daily FORCE ROW LEVEL SECURITY');

        $hasPolicy = DB::selectOne(
            "SELECT 1 AS present FROM pg_policies
              WHERE schemaname = 'gold'
                AND tablename = 'repair_shadow_daily'
                AND policyname = 'repair_shadow_daily_workspace_isolation'",
        );

        if (! $hasPolicy) {
            DB::statement(<<<'SQL'
                CREATE POLICY repair_shadow_daily_workspace_isolation
                    ON gold.repair_shadow_daily
                    USING (
                        workspace_id::text = current_setting('app.workspace_id', true)
                    )
                    WITH CHECK (
                        workspace_id::text = current_setting('app.workspace_id', true)
                    )
            SQL);
        }

        $hasAppRole = DB::selectOne(
            "SELECT 1 AS present FROM pg_roles WHERE rolname = 'georag_app'",
        );

        if ($hasAppRole) {
            DB::statement('GRANT SELECT, INSERT, UPDATE ON gold.repair_shadow_daily TO georag_app');
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP TABLE IF EXISTS gold.repair_shadow_daily');
    }
};
