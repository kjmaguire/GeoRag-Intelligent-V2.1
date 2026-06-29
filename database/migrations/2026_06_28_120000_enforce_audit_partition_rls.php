<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Audit 2026-06-27 IND-2 — pg_partman audit_ledger partitions drift to RLS-OFF.
 *
 * The parent `audit.audit_ledger` has RLS enabled + forced with the
 * `tenant_isolation` policy, so all access THROUGH the parent is filtered.
 * But pg_partman 5.4.3 does NOT propagate RLS from its template to new
 * child partitions (verified empirically: enabling RLS on
 * `partman.template_audit_audit_ledger` still produced an RLS-off
 * `audit_ledger_p20261101`). So every freshly-created monthly partition has
 * its OWN RLS disabled, leaving DIRECT-partition access unfiltered
 * (defense-in-depth gap; the parent policy still covers the normal app path).
 *
 * Fix (three parts, all idempotent + guarded so the test DB no-ops):
 *   1. Enable RLS + FORCE on the template (best-practice; harmless).
 *   2. `audit.enforce_audit_partition_rls()` — enables RLS + FORCE on every
 *      existing child partition missing it; run once here to backfill.
 *   3. An event trigger that runs the same enforcement on every new audit
 *      partition CREATE TABLE — fail-SAFE (a failure RAISEs WARNING, never
 *      aborts pg_partman maintenance).
 */
return new class extends Migration
{
    public function getConnection(): ?string
    {
        // DDL here (ALTER ... ROW LEVEL SECURITY, CREATE EVENT TRIGGER) needs
        // the owner/superuser role. On the SQLite test connection there is no
        // pgsql_migrations server; fall back so the guard no-ops cleanly.
        return config('database.default') === 'sqlite' ? null : 'pgsql_migrations';
    }

    public function up(): void
    {
        if (config('database.default') === 'sqlite') {
            return; // pg_partman / event triggers are PostgreSQL-only.
        }

        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                -- Guard: only run where the pg_partman template exists (skips
                -- a partman-less test/CI database without error).
                IF to_regclass('partman.template_audit_audit_ledger') IS NULL THEN
                    RAISE NOTICE 'enforce_audit_partition_rls: pg_partman template absent — skipping';
                    RETURN;
                END IF;

                -- 1. Template: enable RLS + FORCE (best-practice; pg_partman
                --    does not propagate it, but keep the template honest).
                ALTER TABLE partman.template_audit_audit_ledger ENABLE ROW LEVEL SECURITY;
                ALTER TABLE partman.template_audit_audit_ledger FORCE ROW LEVEL SECURITY;
            END
            $$;
        SQL);

        // 2. Idempotent enforcement function over all child partitions.
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION audit.enforce_audit_partition_rls()
            RETURNS integer
            LANGUAGE plpgsql
            AS $$
            DECLARE
                part   regclass;
                fixed  integer := 0;
            BEGIN
                FOR part IN
                    SELECT inhrelid::regclass
                    FROM pg_inherits
                    WHERE inhparent = 'audit.audit_ledger'::regclass
                LOOP
                    IF NOT (SELECT relrowsecurity FROM pg_class WHERE oid = part) THEN
                        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', part);
                        fixed := fixed + 1;
                    END IF;
                    IF NOT (SELECT relforcerowsecurity FROM pg_class WHERE oid = part) THEN
                        EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', part);
                    END IF;
                END LOOP;
                RETURN fixed;
            END
            $$;
        SQL);

        // 2b. Backfill: enforce on the partitions that already drifted off
        // (e.g. p20260901 / p20261001 created by an earlier maintenance run).
        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF to_regclass('audit.audit_ledger') IS NOT NULL THEN
                    PERFORM audit.enforce_audit_partition_rls();
                END IF;
            END
            $$;
        SQL);

        // 3. Event trigger: auto-enforce on every new audit partition.
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION audit.enforce_new_partition_rls()
            RETURNS event_trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                obj record;
            BEGIN
                FOR obj IN
                    SELECT * FROM pg_event_trigger_ddl_commands()
                    WHERE command_tag = 'CREATE TABLE'
                LOOP
                    IF obj.schema_name = 'audit'
                       AND obj.object_identity LIKE 'audit.audit_ledger_p%' THEN
                        -- Fail-SAFE: never abort the surrounding pg_partman
                        -- maintenance transaction on a privilege hiccup.
                        BEGIN
                            EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', obj.object_identity);
                            EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', obj.object_identity);
                        EXCEPTION WHEN OTHERS THEN
                            RAISE WARNING 'enforce_new_partition_rls: could not set RLS on %: %',
                                obj.object_identity, SQLERRM;
                        END;
                    END IF;
                END LOOP;
            END
            $$;
        SQL);

        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_event_trigger WHERE evtname = 'audit_partition_rls_enforce'
                ) THEN
                    CREATE EVENT TRIGGER audit_partition_rls_enforce
                        ON ddl_command_end
                        WHEN TAG IN ('CREATE TABLE')
                        EXECUTE FUNCTION audit.enforce_new_partition_rls();
                END IF;
            END
            $$;
        SQL);
    }

    public function down(): void
    {
        if (config('database.default') === 'sqlite') {
            return;
        }

        // Drop the trigger + functions. Leave the partitions' RLS ENABLED —
        // turning tenant isolation back OFF on a rollback would be a security
        // regression, not a clean revert.
        DB::statement('DROP EVENT TRIGGER IF EXISTS audit_partition_rls_enforce');
        DB::statement('DROP FUNCTION IF EXISTS audit.enforce_new_partition_rls()');
        DB::statement('DROP FUNCTION IF EXISTS audit.enforce_audit_partition_rls()');
    }
};
