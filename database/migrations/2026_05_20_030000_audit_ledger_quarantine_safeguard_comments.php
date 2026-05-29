<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Operator-visible safeguards for the 2026-05-19 hash-chain fork
 * quarantine.
 *
 * Why this exists
 * ---------------
 * `docs/runbooks/audit_ledger_rehash_2026_05_19.md` records the call:
 * **leave quarantined; do NOT rewrite chain history**. The 1,171 rows
 * in `audit.audit_ledger_chain_fork_quarantine` are tagged with the
 * advisory-lock-incident context and the chain verifier
 * (`app.audit.chain_verify.verify_chain_window`) now skips them as
 * known-divergent.
 *
 * Any future maintenance — partition compaction, cold-tier archive
 * pruning, dev-environment resets — that touches
 * `audit.audit_ledger` MUST preserve both:
 *   1. The quarantined rows themselves (do not DELETE them).
 *   2. The quarantine table mapping.
 *
 * Otherwise the verifier loses its "this fork is recorded, not
 * tampering" signal and starts flagging the divergence as a real
 * integrity break.
 *
 * This migration is purely documentation-via-COMMENT-ON. Operators
 * running `\d+ audit.audit_ledger` in psql will see the constraint
 * before they reach for `DELETE FROM`.
 *
 * SQLite (test DB) — gated on Postgres.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            COMMENT ON TABLE audit.audit_ledger IS
              'Tamper-evident append-only ledger. DO NOT DELETE rows that appear in audit.audit_ledger_chain_fork_quarantine — those are the 1,171 pre-2026-05-19-fix forks the chain verifier expects to find. See docs/runbooks/audit_ledger_rehash_2026_05_19.md.'
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON TABLE audit.audit_ledger_chain_fork_quarantine IS
              'Records hash-chain divergences that are known-expected rather than tampering. Populated 2026-05-19 from the advisory-lock-incident forks. The chain verifier (app.audit.chain_verify) LEFT JOINs this table and skips matching audit_ledger rows. DO NOT TRUNCATE / DROP without first updating the verifier — any deletion would re-classify those rows as integrity breaks.'
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN audit.audit_ledger_chain_fork_quarantine.row_id IS
              'Foreign reference to audit.audit_ledger.id (not a hard FK because audit_ledger is partitioned and PK exists only on each partition). Deletion of this row removes the verifier''s "expected-divergent" marker.'
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement('COMMENT ON TABLE audit.audit_ledger IS NULL');
        DB::statement('COMMENT ON TABLE audit.audit_ledger_chain_fork_quarantine IS NULL');
        DB::statement('COMMENT ON COLUMN audit.audit_ledger_chain_fork_quarantine.row_id IS NULL');
    }
};
