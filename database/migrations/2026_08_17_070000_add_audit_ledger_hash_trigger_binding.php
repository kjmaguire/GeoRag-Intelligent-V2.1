<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Bind `audit.compute_audit_hash()` to `audit.audit_ledger` as a
 * BEFORE INSERT trigger.
 *
 * Bug observed
 * ------------
 * `tests/Feature/DecisionIntelligence/RecordDecisionTest::
 * test_happy_path_workflow_enablement` failed the Postgres suite with
 * "Failed asserting that 0 is identical to 32" on `length(hash)`. Root
 * cause: `App\Services\DecisionIntelligence\RecordDecision::record()`
 * back-fills `silver.decision_records.hash` from the hash that
 * `App\Services\Audit\AuditEmitter::emit()` returns, and that hash is
 * supposed to be computed by a BEFORE INSERT trigger on
 * `audit.audit_ledger` — the emitter itself never computes a hash, per
 * its own docblock ("The hash chain is computed by the Postgres
 * BEFORE-INSERT trigger ... this class never computes hashes itself").
 *
 * On real deployments that trigger is installed by
 * `database/raw/phase0/90-audit-hash-chain-trigger.sql`, applied
 * outside the Laravel migration chain. `2026_05_14_140000_provision_
 * audit_schema_for_test_db.php` mirrors only the *table* shape for
 * `georag_test` and explicitly documents skipping the trigger ("No
 * hash trigger — test fixtures insert rows directly when needed").
 * `2026_05_19_180300_audit_ledger_serialize_chain_writes.php` (and its
 * two follow-up migrations) later added `CREATE OR REPLACE FUNCTION
 * audit.compute_audit_hash()` so the function body itself is
 * migration-managed and present in `georag_test` — but no migration
 * ever issued the matching `CREATE TRIGGER`, so the function exists
 * unbound and every INSERT into `audit.audit_ledger` under
 * RefreshDatabase leaves `hash` / `previous_hash` NULL.
 *
 * `RecordDecision::record()` was written correctly against the
 * production contract (a populated `hash`); the test-DB parity gap is
 * what's wrong. This migration closes it by creating the trigger
 * binding, matching the raw-SQL script exactly (idempotent DROP-then-
 * CREATE) so it is also a harmless no-op re-application on
 * deployments where the raw SQL already created the identical
 * trigger.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // audit.compute_audit_hash() calls public.digest() (pgcrypto).
        // Real deployments get the extension from database/raw/phase0/
        // 90-audit-hash-chain-trigger.sql; the test DB has no equivalent
        // bootstrap step, so it must be provisioned here too.
        //
        // Can't just `CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA
        // public` — 2026_08_17_050000_provision_flow_registry_and_
        // notification_senders_for_test_db.php (which runs earlier in the
        // same migrate:fresh batch) creates the extension unqualified,
        // and 2026_05_13_130000_create_decision_intelligence_schema.php
        // (earlier still) leaves the session's search_path set to
        // `silver, public` for the rest of the run — so on a from-empty
        // migrate:fresh the extension lands in `silver`, not `public`,
        // and `IF NOT EXISTS` then makes this a no-op that never moves
        // it. Handle all three states explicitly so `public.digest()`
        // always resolves regardless of what ran before.
        DB::unprepared(<<<'SQL'
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgcrypto') THEN
                    CREATE EXTENSION pgcrypto WITH SCHEMA public;
                ELSIF NOT EXISTS (
                    SELECT 1 FROM pg_extension e
                    JOIN pg_namespace n ON n.oid = e.extnamespace
                    WHERE e.extname = 'pgcrypto' AND n.nspname = 'public'
                ) THEN
                    ALTER EXTENSION pgcrypto SET SCHEMA public;
                END IF;
            END $$;
        SQL);

        DB::unprepared(<<<'SQL'
            DROP TRIGGER IF EXISTS audit_ledger_compute_hash_trg ON audit.audit_ledger;
            CREATE TRIGGER audit_ledger_compute_hash_trg
                BEFORE INSERT ON audit.audit_ledger
                FOR EACH ROW
                EXECUTE FUNCTION audit.compute_audit_hash();
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::unprepared(
            'DROP TRIGGER IF EXISTS audit_ledger_compute_hash_trg ON audit.audit_ledger;',
        );
    }
};
