<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Let `georag_app` DELETE from audit.query_audit_log, so retention_sweep can
 * purge it (database audit 2026-10).
 *
 * ## The failure
 *
 * `retention_sweep` (src/fastapi/app/hatchet_workflows/retention_sweep.py,
 * `_QUERY_AUDIT_BATCH_SQL`) deletes rows older than QUERY_AUDIT_RETENTION_DAYS
 * from audit.query_audit_log, and then purges silver.ingest_progress. On AWS
 * the worker connects as `georag_app`, and the privilege matrix
 * (2026_08_19_050000_grant_georag_app_privileges_on_migrated_objects) gives
 * that role `INSERT, SELECT, UPDATE` on the whole `audit` schema and no DELETE
 * anywhere in it. So the first statement raises
 * `permission denied for table query_audit_log`, the task fails, and the
 * ingest_progress purge after it never runs -- both tables then grow without
 * bound, which is the exact condition the sweep was written to end
 * (2026-08-14 DB audit item M1). Compose never showed it: the worker is the
 * `georag` superuser there.
 *
 * ## Why DELETE is safe on THIS table
 *
 * audit.query_audit_log is a plain RAG query log, not part of the hash-chained
 * ledger. It has no `hash`/`previous_hash` column and no trigger, nothing
 * verifies it, and nothing chains to it (`audit.verify_hash_chain` reads only
 * audit.audit_ledger). The tamper-evident record is audit.audit_ledger, which
 * stays append-only for this role (2026_10_10_100200). Retention here is a
 * stated policy (NI 43-101 traceability needs a bounded online window), not a
 * loss of evidence.
 *
 * A grant does not bypass RLS. The table is under FORCE ROW LEVEL SECURITY; the
 * sweep is cross-tenant maintenance and runs with no workspace bound, which is
 * only permitted by the table's own fail-open policy
 * (`query_audit_log_workspace_isolation`). If that policy is ever tightened,
 * the purge will delete nothing rather than something it should not -- a
 * silent no-op, so retention_sweep is on the list of unbound readers to move
 * to a per-workspace pass first.
 *
 * ## Guards
 *
 * A no-op where `georag_app` does not exist (a throwaway database that never
 * provisioned the role), where the table does not exist, or where the
 * migrating role cannot grant on it (not the owner, not a member of the owner
 * role). The last case is reported with a NOTICE rather than aborting the
 * deploy, matching 2026_08_19_050000: the grant is the only thing missing and
 * the log says so.
 *
 * Reversible: down() revokes it again.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::unprepared(<<<'SQL'
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app') THEN
                    RAISE NOTICE 'georag_app does not exist; not granting DELETE on audit.query_audit_log';
                    RETURN;
                END IF;

                IF to_regclass('audit.query_audit_log') IS NULL THEN
                    RAISE NOTICE 'audit.query_audit_log does not exist; nothing to grant on';
                    RETURN;
                END IF;

                IF NOT pg_has_role(
                    current_user,
                    (SELECT relowner FROM pg_class WHERE oid = 'audit.query_audit_log'::regclass),
                    'USAGE'
                ) THEN
                    RAISE NOTICE 'skipping DELETE grant on audit.query_audit_log: % does not own it', current_user;
                    RETURN;
                END IF;

                GRANT DELETE ON audit.query_audit_log TO georag_app;
            END
            $$;
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::unprepared(<<<'SQL'
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app')
                   AND to_regclass('audit.query_audit_log') IS NOT NULL
                   AND pg_has_role(
                        current_user,
                        (SELECT relowner FROM pg_class WHERE oid = 'audit.query_audit_log'::regclass),
                        'USAGE'
                   )
                THEN
                    REVOKE DELETE ON audit.query_audit_log FROM georag_app;
                END IF;
            END
            $$;
        SQL);
    }
};
