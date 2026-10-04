<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Index for external_notification's duplicate-delivery check
 * (database audit 2026-10).
 *
 * hatchet_workflows/external_notification.py `_already_recorded` runs, on every
 * inbound notification,
 *
 *     SELECT id::text FROM audit.audit_ledger
 *      WHERE action_type = 'external_notification.received'
 *        AND payload->>'notification_id' = $1
 *      ORDER BY created_at DESC LIMIT 1
 *
 * The only index that mentions action_type is (action_type, created_at DESC),
 * which finds every `external_notification.received` row in the partition set
 * and then filters the JSON out of each -- a scan of all notifications ever
 * received, growing with every one.
 *
 * Partial on that one action_type and keyed on the JSON field, so it holds only
 * notification rows (a tiny fraction of the ledger) and costs nothing on every
 * other audit insert, which matters because the hash-chain trigger already
 * serialises those. `created_at DESC` is the trailing column so the ORDER BY ...
 * LIMIT 1 is satisfied by the index order. The predicate is a literal the query
 * also uses, so the planner proves it implies the index's WHERE.
 *
 * audit.audit_ledger is range-partitioned in production, and CREATE INDEX
 * CONCURRENTLY is not supported on a partitioned parent, so this is an
 * ordinary CREATE INDEX: it holds a SHARE lock (blocking audit writes) while it
 * scans the table. Run in a transaction with a lock_timeout so a long-running
 * writer makes the migration fail and retry rather than queue behind it and
 * stall every audit insert behind the queued lock request. On a deployment
 * whose ledger is large, run it in a quiet window.
 *
 * pgsql only. IF NOT EXISTS: a no-op on re-run.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (! DB::selectOne("SELECT to_regclass('audit.audit_ledger') IS NOT NULL AS present")->present) {
            return;
        }

        DB::statement("SET LOCAL lock_timeout = '30s'");
        DB::statement(
            "CREATE INDEX IF NOT EXISTS audit_ledger_external_notification_id_idx
                 ON audit.audit_ledger ((payload->>'notification_id'), created_at DESC)
              WHERE action_type = 'external_notification.received'",
        );
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP INDEX IF EXISTS audit.audit_ledger_external_notification_id_idx');
    }
};
