<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Serialize audit-ledger chain writes per workspace via an advisory lock.
 *
 * Bug observed
 * ------------
 * `audit.compute_audit_hash` was relying on `SELECT … FOR UPDATE` of the
 * latest row to serialise concurrent inserts into the same workspace's
 * chain. During a 2026-05-16/17 report-build burst on workspace
 * `a0000000-0000-0000-0000-000000000001` (action_type =
 * `report.build.planned`/`report.build.section.drafted`), 1,171 rows
 * landed with stored `previous_hash` values that do not match the actual
 * lagged hash of the same workspace's chain — i.e. multiple concurrent
 * writers chained off the SAME parent hash (verified: up to 4 rows
 * sharing identical `previous_hash`). Forks like that indicate the row
 * lock did not block the second writer.
 *
 * Likely cause: under READ COMMITTED on a partitioned parent table, the
 * trigger's `SELECT … FOR UPDATE LIMIT 1` against the parent did not
 * always produce the lock-and-wait behaviour needed to fence concurrent
 * inserts during high-frequency same-workspace bursts.
 *
 * Fix
 * ---
 * Take a per-workspace advisory transaction lock at the top of the
 * trigger:
 *
 *   PERFORM pg_advisory_xact_lock(hashtextextended(
 *       'audit_chain_' || COALESCE(NEW.workspace_id::text, 'system'), 0));
 *
 * `pg_advisory_xact_lock` blocks any other transaction that asks for the
 * same lock key until the current transaction commits or rolls back.
 * Keying on the workspace id means cross-workspace inserts still run in
 * parallel; only same-workspace chain writes serialise. The lock is
 * released automatically at transaction end so application code does
 * not need to manage it.
 *
 * This change does NOT re-hash the historical 1,171 broken rows — that
 * remediation is a separate one-shot script (see
 * docs/runbooks/audit_ledger_rehash_2026_05_19.md). The fix here stops
 * the bleeding so any future burst writes a coherent chain.
 *
 * SQLite test DB has no audit schema — gated on Postgres.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION audit.compute_audit_hash()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            DECLARE
                v_prev_hash bytea;
                v_message   text;
            BEGIN
                -- Serialise concurrent inserts to the same workspace's chain.
                -- The 2026-05-16 report-build burst proved that row-level
                -- FOR UPDATE alone does not fence concurrent writers under
                -- partition-parent contention. The advisory lock is keyed on
                -- workspace_id so cross-workspace traffic stays parallel.
                PERFORM pg_advisory_xact_lock(
                    hashtextextended(
                        'audit_chain_'
                        || COALESCE(NEW.workspace_id::text, 'system'),
                        0
                    )
                );

                -- Now safe to read the latest row: any concurrent writer
                -- in this workspace is blocked behind us on the lock above.
                -- The FOR UPDATE here is belt-and-braces; the advisory lock
                -- is the load-bearing serialiser.
                SELECT hash INTO v_prev_hash
                FROM audit.audit_ledger
                WHERE (workspace_id IS NOT DISTINCT FROM NEW.workspace_id)
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                FOR UPDATE;

                NEW.previous_hash := v_prev_hash;

                v_message := COALESCE(encode(v_prev_hash, 'hex'), '')
                          || '|' || COALESCE(NEW.actor_id::text, '')
                          || '|' || COALESCE(NEW.actor_kind, '')
                          || '|' || NEW.action_type
                          || '|' || COALESCE(NEW.target_schema, '')
                          || '|' || COALESCE(NEW.target_table, '')
                          || '|' || COALESCE(NEW.target_id, '')
                          || '|' || NEW.payload::text
                          || '|' || to_char(NEW.created_at AT TIME ZONE 'UTC',
                                            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');

                -- Schema-qualify digest() so it resolves even when the
                -- session search_path excludes public (PgBouncer pooling).
                NEW.hash := public.digest(v_message, 'sha256');
                RETURN NEW;
            END $function$;
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION audit.compute_audit_hash()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            DECLARE
                v_prev_hash bytea;
                v_message   text;
            BEGIN
                SELECT hash INTO v_prev_hash
                FROM audit.audit_ledger
                WHERE (workspace_id IS NOT DISTINCT FROM NEW.workspace_id)
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                FOR UPDATE;

                NEW.previous_hash := v_prev_hash;

                v_message := COALESCE(encode(v_prev_hash, 'hex'), '')
                          || '|' || COALESCE(NEW.actor_id::text, '')
                          || '|' || COALESCE(NEW.actor_kind, '')
                          || '|' || NEW.action_type
                          || '|' || COALESCE(NEW.target_schema, '')
                          || '|' || COALESCE(NEW.target_table, '')
                          || '|' || COALESCE(NEW.target_id, '')
                          || '|' || NEW.payload::text
                          || '|' || to_char(NEW.created_at AT TIME ZONE 'UTC',
                                            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');

                NEW.hash := public.digest(v_message, 'sha256');
                RETURN NEW;
            END $function$;
        SQL);
    }
};
