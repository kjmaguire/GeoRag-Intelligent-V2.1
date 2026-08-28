<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Port two more raw-only silver tables into the migration chain.
 *
 *   silver.claim_ledger        §7.4 — one row per claim the LLM makes
 *   silver.workspace_settings  Phase H4 — per-workspace prefs, incl. the
 *                              allow_external_llm egress flag
 *
 * Declared only in `database/raw/phase0/110-section7-4-claim-ledger.sql` and
 * `database/raw/phase0/101-phase-h4-ui-tables.sql`, neither of which CD runs.
 *
 * ## Both have live consumers, and one of them fails closed today
 *
 * `services/claim_ledger.py` is a complete service — two INSERT paths, an
 * UPDATE for verifier results, and two SELECTs — and `routers/answer_runs.py`
 * joins the table for the `claim_ledger` verification rollup it returns to the
 * client. Every one of those is a `42P01 undefined_table` on Azure.
 *
 * `workspace_settings` is worse than unavailable. `agent/egress_gate.py`
 * reads `extra_payload->>'allow_external_llm'` to decide whether a workspace
 * may call an external LLM, and its lookup is wrapped so that ANY exception —
 * the missing table included — returns None, which the caller treats as a hard
 * refuse. The gate is therefore working exactly as designed while being fed by
 * a table that does not exist: external-LLM egress is universally denied on
 * production, and the only trace is a WARN line per call. That is the safe
 * direction to fail, so this migration does not change today's behaviour for
 * any workspace — it makes the flag settable at all.
 *
 * ## A stale justification, corrected
 *
 * The header of `raw/phase0/101` says "The router code in
 * app/routers/admin_tier234.py also performs the same CREATE IF NOT EXISTS on
 * first call so dev/test environments work without running this migration".
 * That is no longer true: `admin_tier234.py` contains no `CREATE TABLE` at
 * all, and no module under `src/` issues one for these tables. The comment
 * describes a runtime-DDL fallback that has since been removed, and it is part
 * of why the table looked safe to leave raw-only.
 *
 * `2026_05_14_140200_provision_phase_h4_ui_tables_for_test_db` mirrors the
 * sibling `silver.qp_credentials` into the test DB and explicitly skips this
 * one — "silver.workspace_settings — no Laravel migration references it". That
 * was accurate then and is what this migration supersedes; `qp_credentials` is
 * deliberately left where it is, since that migration already creates it.
 *
 * ## RLS: two different shapes, both deliberate
 *
 * `workspace_settings` is FAIL-CLOSED, and that is pinned by contract:
 * `src/fastapi/tests/test_workspace_settings_rls_integration.py` asserts an
 * unset GUC sees NO rows and that a write under B's scope naming A's
 * workspace is refused by WITH CHECK. Both arms are `workspace_id =
 * NULLIF(current_setting('app.workspace_id', true), '')::uuid` with no
 * fail-open branch. The raw file already carries FORCE here.
 *
 * `claim_ledger` keeps the raw file's mixed shape — fail-open on read, strict
 * on write. `2026_05_25_185013_normalize_layered_workspace_isolation_policies_phase2`
 * names it in its docblock, so it was checked rather than assumed: that
 * migration is catalog-driven and rewrites a policy only when its `qual`
 * contains three or more `NULLIF` occurrences. This policy's USING has exactly
 * two, so it falls under the threshold and would not have been rewritten. It
 * is also worth not adopting that migration's target shape here even if it
 * had: the normalized form emits USING only, letting WITH CHECK default to the
 * fail-open USING expression, whereas the raw policy's explicit WITH CHECK
 * keeps writes strict. Porting verbatim is both the accurate reconciliation
 * and the tighter one.
 *
 * Tightening the read side of `claim_ledger` belongs with the tiered work in
 * `docs/architecture/fail-open-rls-posture-2026-08-21.md`, not here.
 *
 * ## FORCE
 *
 * The raw file gives `claim_ledger` `ENABLE ROW LEVEL SECURITY` with no
 * `FORCE`. The catalog sweep in `2026_08_24_010000` is one-shot and already
 * ran, so a table created afterwards with ENABLE alone leaves the owner
 * bypassing its own policy — which
 * `WorkspaceRlsCoverageTest::test_every_rls_enabled_table_is_forced` asserts
 * against. FORCE is added. `workspace_settings` already has it in raw.
 *
 * ## Index coverage
 *
 * Both satisfy the `gate="index"` check in
 * `routers/audit_findings.py::get_tenant_isolation_findings`, which flags a
 * `silver` table carrying `workspace_id` with no index mentioning it:
 * `claim_ledger` via `idx_claim_ledger_workspace`, `workspace_settings` via
 * its primary key, which IS `workspace_id`.
 *
 * Idempotent: `IF NOT EXISTS` throughout, `DROP POLICY IF EXISTS` before each
 * `CREATE POLICY`.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // ── silver.claim_ledger ───────────────────────────────────────────
        // The structured complement to silver.answer_citation_items: that
        // records citations, this records the CLAIMS plus the kind of
        // evidence each one requires. No FK on answer_run_id in the raw
        // file, and none added — claims are written during a run, before
        // the run row is necessarily durable.
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.claim_ledger (
                claim_id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id           uuid NOT NULL,
                answer_run_id          uuid NOT NULL,
                claim_text             text NOT NULL,
                claim_type             varchar(32) NOT NULL,
                required_support_type  varchar(32) NOT NULL,
                verification_status    varchar(16) NOT NULL DEFAULT 'pending',
                verifier               varchar(64),
                verifier_evidence_json jsonb,
                confidence_score       numeric(5,3),
                source_passage_id      uuid,
                sequence_in_answer     int,
                created_at             timestamptz NOT NULL DEFAULT now(),
                updated_at             timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT claim_type_valid CHECK (
                    claim_type IN (
                        'numeric','entity','temporal','spatial',
                        'relationship','refusal','qualitative'
                    )
                ),
                CONSTRAINT claim_support_valid CHECK (
                    required_support_type IN (
                        'citation','structured_row','computation','none'
                    )
                ),
                CONSTRAINT claim_verification_valid CHECK (
                    verification_status IN (
                        'pending','verified','failed','skipped','insufficient'
                    )
                )
            )
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_claim_ledger_answer_run
                ON silver.claim_ledger (answer_run_id)
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_claim_ledger_workspace
                ON silver.claim_ledger (workspace_id, created_at DESC)
        SQL);
        // Partial: the ledger is read to find work still needing verification,
        // so the verified rows (the eventual majority) stay out of the index.
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_claim_ledger_status
                ON silver.claim_ledger (verification_status)
             WHERE verification_status != 'verified'
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_claim_ledger_type
                ON silver.claim_ledger (claim_type)
        SQL);

        DB::statement('ALTER TABLE silver.claim_ledger ENABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE silver.claim_ledger FORCE ROW LEVEL SECURITY');
        DB::statement('DROP POLICY IF EXISTS claim_ledger_ws_isolation ON silver.claim_ledger');
        DB::statement(<<<'SQL'
            CREATE POLICY claim_ledger_ws_isolation ON silver.claim_ledger
                USING (
                    workspace_id = (NULLIF(current_setting('app.workspace_id', true), '')::uuid)
                    OR NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                )
                WITH CHECK (
                    workspace_id = (NULLIF(current_setting('app.workspace_id', true), '')::uuid)
                )
        SQL);

        // ── silver.workspace_settings ─────────────────────────────────────
        // workspace_id is BOTH the primary key and the FK — one settings row
        // per workspace, and the PK doubles as the workspace_id index the
        // §11.5 gate looks for.
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.workspace_settings (
                workspace_id        uuid PRIMARY KEY
                    REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                default_tone        text NOT NULL DEFAULT 'technical',
                default_report_type text,
                sla_max_response_ms integer,
                extra_payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
                created_at          timestamptz NOT NULL DEFAULT now(),
                updated_at          timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT workspace_settings_default_tone_check
                    CHECK (default_tone IN ('technical', 'executive', 'regulator')),
                CONSTRAINT workspace_settings_sla_max_response_ms_check
                    CHECK (sla_max_response_ms IS NULL OR sla_max_response_ms > 0)
            )
        SQL);
        // Redundant with the primary key, but ported because the raw file has
        // it and dropping it would be a silent divergence rather than a
        // decision. Postgres will simply never choose it over the PK.
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_workspace_settings_workspace_id
                ON silver.workspace_settings (workspace_id)
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON TABLE silver.workspace_settings IS
                'Phase H4 — per-workspace UI/agent preferences. default_tone drives §7.6 Presentation Coach.'
        SQL);

        DB::statement('ALTER TABLE silver.workspace_settings ENABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE silver.workspace_settings FORCE ROW LEVEL SECURITY');
        DB::statement(<<<'SQL'
            DROP POLICY IF EXISTS workspace_settings_workspace_isolation
                ON silver.workspace_settings
        SQL);
        // Fail-closed on BOTH arms — pinned by
        // src/fastapi/tests/test_workspace_settings_rls_integration.py.
        DB::statement(<<<'SQL'
            CREATE POLICY workspace_settings_workspace_isolation ON silver.workspace_settings
                USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
                WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
        SQL);

        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app') THEN
                    GRANT USAGE ON SCHEMA silver TO georag_app;
                    GRANT SELECT, INSERT, UPDATE, DELETE
                        ON silver.claim_ledger, silver.workspace_settings
                        TO georag_app;
                END IF;
            END $$;
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP TABLE IF EXISTS silver.workspace_settings');
        DB::statement('DROP TABLE IF EXISTS silver.claim_ledger');
    }
};
