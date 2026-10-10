<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Make audit.audit_ledger append-only for the application role (database audit
 * 2026-10, LOW / security).
 *
 * ## The gap
 *
 * `georag_app` -- the role every ECS service connects as -- held
 * `INSERT, SELECT, UPDATE` on audit.audit_ledger (the 2026_08_19_050000
 * matrix grants UPDATE on the whole `audit` schema), and the only trigger on the
 * table was the BEFORE INSERT hash trigger. A role with UPDATE can rewrite a
 * row, and because the trigger only runs on INSERT it can rewrite `hash` and
 * `previous_hash` to match, re-hashing history. The chain is tamper-EVIDENT
 * only against someone who cannot also fix the evidence up.
 * docs/audit_ledger_hash_recipe.md listed this as known limitation 4 ("Phase 11
 * should add a constraint trigger that rejects UPDATE / DELETE, plus revoke
 * those grants from the application role"); this is that.
 *
 * ## Two layers, on purpose
 *
 *  1. REVOKE UPDATE, DELETE ON audit.audit_ledger (and every partition) FROM
 *     `georag_app` and the NOLOGIN grant-holder `georag_write`. A privilege
 *     check fails first and costs nothing.
 *  2. A BEFORE UPDATE OR DELETE row trigger that RAISEs. This is the layer that
 *     survives a re-grant: `database/raw/phase1/10-georag-app-role.sql`
 *     (`GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA audit`), a re-run
 *     of the 2026_08_19_050000 matrix, or a future "GRANT ALL" would each put the
 *     privilege back, and a privilege is all a revoke ever controls. The
 *     application role cannot drop or disable the trigger (that needs table
 *     ownership), and it fires for the owner and for superusers too.
 *
 * The matrix migration itself is amended to withhold UPDATE from this one table,
 * so re-running it no longer undoes layer 1.
 *
 * ## Prerequisite: the hash trigger must not need UPDATE
 *
 * audit.compute_audit_hash() used to end its lookup in `FOR UPDATE`, which
 * PostgreSQL authorises with the UPDATE privilege, as the inserting role.
 * Revoking UPDATE with that body in place makes every audit insert by
 * `georag_app` fail with `permission denied for table audit_ledger` (reproduced
 * on PostgreSQL 16). 2026_10_10_100100 removes the row lock; this migration
 * REFUSES to run if the live function still takes one, so an out-of-order or
 * partial rollout stops here instead of taking audit writes down.
 *
 * ## Who legitimately UPDATEs or DELETEs this table
 *
 * Checked across app/, src/, database/ and the test trees (2026-10-10): nobody,
 * in production code.
 *
 *  - The 2026-05-19 chain-fork quarantine (2026_05_19_180400, 2026_05_20_030000)
 *    is ANNOTATION: it records the divergent row ids in
 *    audit.audit_ledger_chain_fork_quarantine and "NEVER mutates audit history".
 *    It neither updates nor deletes ledger rows, and that table is untouched here.
 *  - audit.run_verification() writes audit.audit_ledger_verification_runs, which
 *    keeps UPDATE for `georag_app`.
 *  - The only DELETE in the tree is `prune_archived_window()` in
 *    src/fastapi/app/audit/cold_tier_archive.py: opt-in retention that nothing
 *    calls (no workflow, route or script imports it), and which the app role
 *    could never run (no DELETE grant). It is now refused for every role. A
 *    deliberate prune must be a break-glass act: a superuser (RDS: rds_superuser)
 *    runs `SET LOCAL session_replication_role = replica` in that transaction,
 *    which stops ordinary triggers firing. Partition-based retention
 *    (pg_partman DROP) is DDL and is not affected.
 *  - Five Python integration modules (test_alerts_inbox_integration,
 *    test_audit_chain_verify, test_cost_burn_watcher, test_cross_workspace_audit,
 *    test_report_section_drafts_integration) clean up their own rows with
 *    `DELETE FROM audit.audit_ledger`, and test_audit_chain_verify forges a
 *    previous_hash with UPDATE to prove the verifier notices, all as the
 *    `georag` superuser. None is in the blocking CI manifest; they use the same
 *    break-glass, changed in the same commit.
 *
 * TRUNCATE is not covered (a statement-level concept; `georag_app` has never held
 * it and the table owner can still use it, so it is an operator action, as is
 * dropping the trigger).
 *
 * down() drops the trigger and function and gives UPDATE back to `georag_app`
 * only (the role the matrix granted it to). It does not re-grant to
 * `georag_write`, which may never have held it on a given cluster. Roll this back
 * BEFORE 2026_10_10_100100, whose own down() reinstates a function that needs
 * UPDATE.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (! $this->ledgerExists()) {
            // The audit schema is provisioned by the migration chain on every
            // cluster; a database without it has nothing to protect.
            return;
        }

        $this->refuseIfHashTriggerStillTakesARowLock();

        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION audit.reject_audit_ledger_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog
            AS $function$
            BEGIN
                RAISE EXCEPTION 'audit.audit_ledger is append-only: % is not permitted (row id %)', TG_OP, OLD.id
                    USING ERRCODE = 'restrict_violation',
                          HINT = 'History is never rewritten. To annotate a row, write to audit.audit_ledger_chain_fork_quarantine. A deliberate, reviewed prune is a superuser act: SET LOCAL session_replication_role = replica in that transaction.';
            END
            $function$;

            COMMENT ON FUNCTION audit.reject_audit_ledger_mutation() IS
                'BEFORE UPDATE OR DELETE trigger function on audit.audit_ledger: always raises. The ledger is append-only; see 2026_10_10_100200_make_audit_ledger_append_only.';

            DROP TRIGGER IF EXISTS audit_ledger_append_only_trg ON audit.audit_ledger;
            CREATE TRIGGER audit_ledger_append_only_trg
                BEFORE UPDATE OR DELETE ON audit.audit_ledger
                FOR EACH ROW
                EXECUTE FUNCTION audit.reject_audit_ledger_mutation();

            COMMENT ON TRIGGER audit_ledger_append_only_trg ON audit.audit_ledger IS
                'Rejects every UPDATE and DELETE. Ordinary triggers do not fire when session_replication_role = replica, which only a superuser can set: that is the break-glass.';
        SQL);

        // Every table in the inheritance tree: a partitioned ledger (raw
        // phase0/20) has children, and a role with UPDATE on a child could
        // otherwise address it directly by name.
        DB::unprepared(<<<'SQL'
            DO $$
            DECLARE
                v_role text;
                v_rel  regclass;
            BEGIN
                FOREACH v_role IN ARRAY ARRAY['georag_app', 'georag_write'] LOOP
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
                        CONTINUE;
                    END IF;

                    FOR v_rel IN
                        WITH RECURSIVE tree(oid) AS (
                            SELECT 'audit.audit_ledger'::regclass::oid
                            UNION ALL
                            SELECT i.inhrelid FROM pg_inherits i JOIN tree t ON i.inhparent = t.oid
                        )
                        SELECT oid::regclass FROM tree
                    LOOP
                        EXECUTE format('REVOKE UPDATE, DELETE ON %s FROM %I', v_rel, v_role);
                    END LOOP;
                END LOOP;
            END
            $$;
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (! $this->ledgerExists()) {
            return;
        }

        DB::unprepared(<<<'SQL'
            DROP TRIGGER IF EXISTS audit_ledger_append_only_trg ON audit.audit_ledger;
            DROP FUNCTION IF EXISTS audit.reject_audit_ledger_mutation();

            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app') THEN
                    GRANT UPDATE ON audit.audit_ledger TO georag_app;
                END IF;
            END
            $$;
        SQL);
    }

    private function ledgerExists(): bool
    {
        return (bool) DB::selectOne("SELECT to_regclass('audit.audit_ledger') IS NOT NULL AS present")->present;
    }

    /**
     * Fail loudly if audit.compute_audit_hash() still locks rows.
     *
     * A row lock (FOR UPDATE / NO KEY UPDATE / SHARE / KEY SHARE) needs the
     * UPDATE privilege this migration removes. Comments are stripped first so a
     * function that merely MENTIONS the clause does not trip it.
     */
    private function refuseIfHashTriggerStillTakesARowLock(): void
    {
        $row = DB::selectOne(
            "SELECT pg_get_functiondef(to_regprocedure('audit.compute_audit_hash()')) AS def
              WHERE to_regprocedure('audit.compute_audit_hash()') IS NOT NULL",
        );

        if ($row === null) {
            return;
        }

        $code = (string) preg_replace('/--[^\n]*/', '', (string) $row->def);

        if (preg_match('/\bFOR\s+(?:NO\s+KEY\s+UPDATE|UPDATE|KEY\s+SHARE|SHARE)\b/i', $code) === 1) {
            throw new RuntimeException(
                'Refusing to revoke UPDATE on audit.audit_ledger: audit.compute_audit_hash() still takes a '
                .'row lock (SELECT ... FOR UPDATE), which needs that privilege, so every audit insert by the '
                .'application role would fail. Apply 2026_10_10_100100_audit_hash_trigger_stamps_created_at_after_chain_lock '
                .'first (and re-run database/raw/phase0/90-audit-hash-chain-trigger.sql if an older copy was applied by hand).',
            );
        }
    }
};
