<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Make the audit hash-chain trigger's "previous hash" lookup indexable
 * (database audit 2026-10, HIGH).
 *
 * audit.compute_audit_hash() (2026_05_19_180300, :86 and :128) found the
 * parent row with
 *
 *     WHERE workspace_id IS NOT DISTINCT FROM NEW.workspace_id
 *     ORDER BY created_at DESC, id DESC LIMIT 1 FOR UPDATE
 *
 * `IS NOT DISTINCT FROM` is not an indexable operator, so every audit insert
 * sequentially scanned audit.audit_ledger and sorted it -- while holding the
 * per-workspace advisory lock, so every other writer to that workspace's
 * chain queued behind it. Measured 57-75 ms per insert at 300k rows, growing
 * linearly with the table.
 *
 * Here the lookup is split into the two cases the operator was merging:
 * `workspace_id IS NULL` (the system-wide chain) and `workspace_id = NEW.workspace_id`.
 * Both are served by audit_ledger_workspace_id_idx (workspace_id, created_at
 * DESC) -- an index probe plus an incremental sort on `id` for rows that share
 * a created_at.
 *
 * ONLY THE LOOKUP CHANGES. The advisory lock, the FOR UPDATE, the message
 * concatenation and the digest are copied byte for byte from 2026_05_19_180300:
 * the hash of every future row is identical to what the old function would
 * have produced, so audit.recompute_hash() / verify_hash_chain() (which mirror
 * that formula) still agree with the stored chain, and the historical rows
 * are untouched. NULL-workspace and non-NULL-workspace chains select exactly
 * the rows `IS NOT DISTINCT FROM` selected.
 *
 * The index is created by 2026_05_14_140000 (test DBs) and
 * database/raw/phase0/20-layer-b-audit-ledger.sql (production); it is only
 * (re)asserted here with IF NOT EXISTS so a cluster that somehow lacks it does
 * not silently regress to the scan. Where it already exists -- every cluster
 * this is expected to run on -- that is a no-op, so no lock is taken; on a
 * partitioned parent a missing index would be a non-concurrent build.
 *
 * down() restores the 2026_05_19_180300 statements (same hash formula, the
 * IS NOT DISTINCT FROM lookup).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (DB::selectOne("SELECT to_regclass('audit.audit_ledger') IS NOT NULL AS present")->present) {
            DB::statement(
                'CREATE INDEX IF NOT EXISTS audit_ledger_workspace_id_idx
                     ON audit.audit_ledger (workspace_id, created_at DESC)',
            );
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
                --
                -- Two branches rather than `workspace_id IS NOT DISTINCT FROM
                -- NEW.workspace_id`: that operator cannot use an index, and
                -- this ran as a sequential scan + sort on every insert.
                -- Each branch is an index probe on
                -- audit_ledger_workspace_id_idx (workspace_id, created_at DESC).
                IF NEW.workspace_id IS NULL THEN
                    -- `workspace_id` leads the ORDER BY on purpose. For an
                    -- `= value` qual the planner knows the column is constant
                    -- and drops it from the sort; for `IS NULL` it does not,
                    -- so without it the index (workspace_id, created_at DESC)
                    -- is not seen as presorted and the planner falls back to
                    -- the very scan + sort this migration removes (measured:
                    -- 56 ms vs 0.25 ms at 300k rows). Every row here has a
                    -- NULL workspace_id, so the extra key orders nothing and
                    -- the row chosen is the same one.
                    SELECT hash INTO v_prev_hash
                    FROM audit.audit_ledger
                    WHERE workspace_id IS NULL
                    ORDER BY workspace_id, created_at DESC, id DESC
                    LIMIT 1
                    FOR UPDATE;
                ELSE
                    SELECT hash INTO v_prev_hash
                    FROM audit.audit_ledger
                    WHERE workspace_id = NEW.workspace_id
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    FOR UPDATE;
                END IF;

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
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // 2026_05_19_180300 up(), verbatim.
        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION audit.compute_audit_hash()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            DECLARE
                v_prev_hash bytea;
                v_message   text;
            BEGIN
                PERFORM pg_advisory_xact_lock(
                    hashtextextended(
                        'audit_chain_'
                        || COALESCE(NEW.workspace_id::text, 'system'),
                        0
                    )
                );

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
