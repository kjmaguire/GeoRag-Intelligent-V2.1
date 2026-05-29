<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Quarantine the 1,171 pre-fix audit-ledger chain forks.
 *
 * Background (full detail in docs/runbooks/audit_ledger_rehash_2026_05_19.md)
 * --------------------------------------------------------------------------
 * Before migration 2026_05_19_180300 replaced the chain-write trigger with
 * a per-workspace advisory-lock implementation, concurrent inserts during
 * the 2026-05-16/17 report-build burst forked workspace
 * `a0000000-0000-0000-0000-000000000001`'s chain — 1,171 rows landed with
 * `previous_hash` values that don't match the lagged actual `hash` of the
 * same workspace's chain (verified: up to 4 rows sharing a parent).
 *
 * Approach: NEVER mutate audit history. Instead, record the divergent rows
 * in a quarantine table the chain integrity verifier consults; the
 * verifier treats quarantined rows as "known pre-fix divergence" rather
 * than "tampering". Auditors can reconstruct the lineage from the
 * quarantine table + the original rows.
 *
 * This migration:
 *   1. Creates `audit.audit_ledger_chain_fork_quarantine`.
 *   2. Populates it by re-running the per-workspace lag-window detector
 *      against current ledger state, capturing every divergent row id.
 *
 * The integrity verifier (Hatchet `audit_ledger_verify` workflow) will
 * be updated in a follow-up to LEFT JOIN against this table and exclude
 * quarantined rows from the broken-link count, so post-fix scans report
 * the clean 0 they should.
 *
 * RLS: rows include a workspace_id column; standard tenant_isolation
 * policy applies (matches `audit.audit_ledger`'s own policy shape).
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

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS audit.audit_ledger_chain_fork_quarantine (
                row_id          uuid        PRIMARY KEY,
                workspace_id    uuid        NULL,
                action_type     text        NULL,
                row_created_at  timestamptz NOT NULL,
                stored_previous_hash bytea  NULL,
                actual_previous_hash bytea  NULL,
                discovered_at   timestamptz NOT NULL DEFAULT now(),
                discovered_in   text        NOT NULL DEFAULT
                                'phase5-quality-eval-2026-05-19',
                fix_migration   text        NOT NULL DEFAULT
                                '2026_05_19_180300_audit_ledger_serialize_chain_writes',
                note            text        NULL
            )
        SQL);

        DB::statement(
            'COMMENT ON TABLE audit.audit_ledger_chain_fork_quarantine IS '
            ."'Rows in audit.audit_ledger whose stored previous_hash does not "
            .'match the lagged actual hash of their workspace chain. '
            .'Recorded once at Phase 5 (2026-05-19) after migration '
            .'2026_05_19_180300 fixed the underlying serialisation bug. '
            ."Never mutate audit history — annotate instead.'",
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS audit_ledger_chain_fork_quarantine_ws_idx '
            .'ON audit.audit_ledger_chain_fork_quarantine (workspace_id, row_created_at)',
        );

        // Populate from current ledger state. Idempotent — uses ON CONFLICT.
        DB::statement(<<<'SQL'
            INSERT INTO audit.audit_ledger_chain_fork_quarantine
                (row_id, workspace_id, action_type, row_created_at,
                 stored_previous_hash, actual_previous_hash)
            SELECT id, workspace_id, action_type, created_at,
                   previous_hash, prev_actual
            FROM (
                SELECT id, workspace_id, action_type, created_at,
                       hash, previous_hash,
                       lag(hash) OVER (
                           PARTITION BY workspace_id
                           ORDER BY created_at, id
                       ) AS prev_actual
                FROM audit.audit_ledger
            ) c
            WHERE c.prev_actual IS NOT NULL
              AND c.previous_hash IS DISTINCT FROM c.prev_actual
            ON CONFLICT (row_id) DO NOTHING
        SQL);

        // RLS — match the audit_ledger pattern.
        DB::statement(
            'ALTER TABLE audit.audit_ledger_chain_fork_quarantine '
            .'ENABLE ROW LEVEL SECURITY',
        );
        DB::statement(<<<'SQL'
            CREATE POLICY chain_fork_quarantine_workspace_isolation
              ON audit.audit_ledger_chain_fork_quarantine
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement(
            'DROP POLICY IF EXISTS chain_fork_quarantine_workspace_isolation '
            .'ON audit.audit_ledger_chain_fork_quarantine',
        );
        DB::statement('DROP TABLE IF EXISTS audit.audit_ledger_chain_fork_quarantine');
    }
};
