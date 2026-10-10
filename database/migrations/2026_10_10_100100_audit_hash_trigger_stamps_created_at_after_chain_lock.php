<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Stamp audit.audit_ledger.created_at INSIDE the hash trigger, after the chain
 * lock, and stop the trigger asking for the UPDATE privilege (database audit
 * 2026-10, two findings that need the same function body).
 *
 * ## 1. created_at was assigned before the lock (chain order could invert)
 *
 * `created_at` defaults to `clock_timestamp()`. A column DEFAULT is evaluated
 * when the tuple is formed, which is BEFORE the BEFORE INSERT trigger fires, and
 * the trigger's first act is `pg_advisory_xact_lock(...)` on the workspace's
 * chain. So under concurrency a writer takes its timestamp, then waits for the
 * lock, and a writer that arrived later but did not have to wait can take the
 * lock first. The late lock-holder links to the chain's current tail (it picks
 * "the row with the greatest (created_at, id)"), and the early-timestamp writer
 * then links to THAT row -- carrying a created_at EARLIER than its own parent.
 *
 * The verifier orders each workspace chain by (created_at, id)
 * (audit.verify_hash_chain, docs/audit_ledger_hash_recipe.md: "The chain order
 * within a scope is (created_at ASC, id ASC)"), so for that pair it expects a
 * different parent than the one stored: a reported break on a chain nobody
 * touched. The 2026-05-16 report-build burst that 2026_05_19_180300 fixed was the
 * same race at a larger scale; the advisory lock closed the double-parent half
 * of it, and this closes the ordering half.
 *
 * Fix: `NEW.created_at := clock_timestamp()` after the lock is held. From then
 * on the order in which writers obtain the lock IS the order of their
 * timestamps, because the lock is held to the end of the writer's transaction:
 * the next writer cannot read the clock until the previous one has committed.
 * The hash message below is built after the assignment, so the stored hash
 * commits to the stored timestamp.
 *
 * The assignment is unconditional, so a caller can no longer choose the ledger's
 * timestamp. Neither production emitter ever did (AuditEmitter.php and
 * app/audit/__init__.py omit the column), and for a tamper-evident ledger a
 * client-chosen "when" is a defect rather than a feature. Tests that need a
 * specific timestamp must not rely on it; see AuditLedgerAppendOnlyTest.
 *
 * ## 2. The trigger needed UPDATE on the table it appends to
 *
 * The previous-row lookup ended in `FOR UPDATE`. PostgreSQL requires UPDATE
 * privilege on a table for SELECT ... FOR UPDATE / FOR SHARE (GRANT docs:
 * "also required for SELECT ... FOR UPDATE and SELECT ... FOR SHARE"), and the
 * trigger runs as the INSERTING role. Revoking UPDATE on audit.audit_ledger from
 * `georag_app` -- which making the ledger append-only requires (next migration)
 * -- would therefore make EVERY audit insert by the application fail with
 * `permission denied for table audit_ledger`. That was reproduced on
 * PostgreSQL 16 before this was written.
 *
 * The row lock is not what serialises the chain; the per-workspace advisory lock
 * is (the 2026_05_19_180300 comment says so: "The FOR UPDATE here is
 * belt-and-braces; the advisory lock is the load-bearing serialiser"), and the
 * advisory lock is held to end of transaction, so the lookup that follows it
 * already sees the previous writer's committed row. Locking the tail row as well
 * protects against a concurrent UPDATE of that row, and nothing is allowed to do
 * that any more. Dropping it changes no hash and no ordering.
 *
 * ## What does not change
 *
 * The signature, the trigger binding (`audit_ledger_compute_hash_trg`, BEFORE
 * INSERT, FOR EACH ROW), the advisory-lock key, the two indexable lookup
 * branches of 2026_10_04_200000 and the hash message. audit.recompute_hash() and
 * audit.verify_hash_chain() mirror the formula and are not touched, so every
 * historical row still verifies.
 *
 * database/raw/phase0/90-audit-hash-chain-trigger.sql carries a copy of this
 * function that is not applied by db:apply-raw but IS applied by hand on local
 * clusters; it is updated in the same change so a raw apply cannot revert this.
 *
 * down() restores the 2026_10_04_200000 definition (FOR UPDATE, no stamping).
 * Roll back 2026_10_10_100200 (append-only) first: with UPDATE revoked, that
 * definition cannot insert as `georag_app`.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
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
                -- The advisory lock is keyed on workspace_id so cross-workspace
                -- traffic stays parallel, and it is held to the end of the
                -- writer's transaction.
                PERFORM pg_advisory_xact_lock(
                    hashtextextended(
                        'audit_chain_'
                        || COALESCE(NEW.workspace_id::text, 'system'),
                        0
                    )
                );

                -- The row's timestamp is taken HERE, after the lock, never from
                -- the column DEFAULT (evaluated before this trigger and so
                -- before any wait for the lock). Whoever holds the lock is the
                -- latest writer on the chain, and now() > the previous writer's
                -- stamp because that writer committed before releasing it.
                NEW.created_at := clock_timestamp();

                -- Two branches rather than `workspace_id IS NOT DISTINCT FROM
                -- NEW.workspace_id`, which cannot use an index (2026_10_04_200000).
                -- Each is an index probe on audit_ledger_workspace_id_idx
                -- (workspace_id, created_at DESC).
                --
                -- No row lock on the tail row: the advisory lock above is the
                -- serialiser, and a row lock needs UPDATE privilege on the
                -- table, which the application role must not hold on an
                -- append-only ledger.
                IF NEW.workspace_id IS NULL THEN
                    -- `workspace_id` leads the ORDER BY on purpose: for an
                    -- `= value` qual the planner knows the column is constant
                    -- and drops it from the sort; for `IS NULL` it does not,
                    -- so without it the index is not seen as presorted and the
                    -- planner falls back to the scan + sort 2026_10_04_200000
                    -- removed. Every row here has a NULL workspace_id, so the
                    -- extra key orders nothing.
                    SELECT hash INTO v_prev_hash
                    FROM audit.audit_ledger
                    WHERE workspace_id IS NULL
                    ORDER BY workspace_id, created_at DESC, id DESC
                    LIMIT 1;
                ELSE
                    SELECT hash INTO v_prev_hash
                    FROM audit.audit_ledger
                    WHERE workspace_id = NEW.workspace_id
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1;
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

                -- Schema-qualify digest() so it resolves even when the session
                -- search_path excludes public (PgBouncer pooling).
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

        // 2026_10_04_200000 up(), verbatim.
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

                IF NEW.workspace_id IS NULL THEN
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

                NEW.hash := public.digest(v_message, 'sha256');
                RETURN NEW;
            END $function$;
        SQL);
    }
};
