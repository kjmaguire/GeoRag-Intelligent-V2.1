<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * DB dimension push-to-9.5 sweep (2026-08-15) — closes a raw-SQL-vs-
 * migration parity gap one level worse than the usual pattern: unlike
 * `database/raw/phase0/*.sql` objects that at least exist SOMEWHERE in
 * version control, `silver.pdf_vl_summaries` has NO CREATE TABLE anywhere
 * in this repository — not in `database/migrations/`, not in
 * `database/raw/` (including `_archive/`) — despite being actively read
 * and written by `app/services/pdf_vl.py`:
 *
 *   - `_cache_hit()`  (pdf_vl.py:747-756) SELECTs summary_id, section_ref,
 *     summary_text, claims, model_id, model_backend, mean_claim_confidence,
 *     prompt_tokens, completion_tokens WHERE workspace_id/pdf_id/
 *     section_ref_hash/model_id.
 *   - `_persist()` (pdf_vl.py:804-826) INSERTs the same columns plus
 *     workspace_id, section_ref, extracted_at, with
 *     `ON CONFLICT (pdf_id, section_ref_hash, model_id) DO NOTHING`.
 *
 * The table is nonetheless treated as real elsewhere in the codebase:
 *   - `database/raw/phase0/97-rls-tenant-isolation-block2.sql` and
 *     `100-rls-tenant-isolation-block4.sql` both list it for tenant-
 *     isolation RLS.
 *   - `2026_08_14_030000_close_rls_admin_escape_hatch_verified_subset.php`
 *     targets policy `pdf_vl_summaries_workspace_isolation` on it — guarded
 *     by `tableExists()`, so it silently no-ops when the table is absent
 *     (same escape valve already used for the raw-SQL-only
 *     workspace.workspace_memberships etc.).
 *   - `tests/Feature/Tenancy/WorkspaceRlsCoverageTest.php` expects that
 *     exact policy and explicitly comments that a missing row here is
 *     "not a regression to flag" — i.e. the gap was already known and
 *     silently tolerated, not fixed.
 *
 * Net effect: on any migrate-only Postgres (CI's test DB, a fresh dev
 * clone, a disaster-recovery restore from schema-only backup), every VL
 * section-summary cache read/write throws `relation
 * "silver.pdf_vl_summaries" does not exist` — §04p Stage 6 (Qwen-VL
 * section summarisation) is silently broken outside whatever environment
 * originally got this table created out-of-band.
 *
 * `CREATE TABLE IF NOT EXISTS` makes this a safe no-op if the table
 * already exists (e.g. on live prod, created by whatever out-of-band
 * process actually put it there) — this migration only fills the gap on
 * environments where it's genuinely missing. Column shapes are transcribed
 * exactly from the SQL pdf_vl.py already issues, so a correctly-shaped
 * production table is unaffected either way. The RLS ENABLE/POLICY and
 * CREATE INDEX statements below are idempotent regardless (DROP POLICY IF
 * EXISTS + CREATE POLICY, CREATE INDEX IF NOT EXISTS), so re-running them
 * against an already-correct production table is harmless.
 *
 * RLS policy is written directly in the CURRENT fail-closed shape (no
 * `IS NULL OR` escape hatch) rather than the older fail-open pattern
 * 2026_05_25_173814 used and 2026_08_14_030000 later had to convert away
 * from — no reason to introduce a new table already carrying the
 * documented security debt.
 *
 * model_backend CHECK excludes 'ollama' from the start — see
 * app/models/pdf.py VlBackend for the matching Literal fix (same 2026-08-15
 * sweep) and app/services/pdf_vl.py's own docstring, which has only ever
 * documented PDF_VL_BACKEND as vllm|anthropic.
 *
 * Test-DB parity: this IS the provision migration — no separate
 * `*_provision_*_for_test_db` sibling needed, this creates the table
 * outright for every pgsql environment including the test DB. Skipped on
 * sqlite (silver.* + RLS don't exist there).
 */
return new class extends Migration
{
    private const POLICY = 'pdf_vl_summaries_workspace_isolation';

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.pdf_vl_summaries (
                summary_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id        uuid NOT NULL,
                pdf_id              char(64) NOT NULL,
                section_ref         jsonb NOT NULL,
                section_ref_hash    text NOT NULL,
                summary_text        text NOT NULL,
                claims              jsonb NOT NULL,
                model_id            text NOT NULL,
                model_backend       text NOT NULL,
                mean_claim_confidence real,
                prompt_tokens       integer,
                completion_tokens   integer,
                extracted_at        timestamptz NOT NULL DEFAULT now(),

                CONSTRAINT chk_pdf_vl_summaries_pdf_id_hex
                    CHECK (pdf_id ~ '^[0-9a-f]{64}$'),
                CONSTRAINT chk_pdf_vl_summaries_backend
                    CHECK (model_backend IN ('vllm', 'anthropic')),
                CONSTRAINT chk_pdf_vl_summaries_confidence
                    CHECK (mean_claim_confidence IS NULL
                           OR (mean_claim_confidence >= 0 AND mean_claim_confidence <= 1)),

                CONSTRAINT uq_pdf_vl_summaries_pdf_section_model
                    UNIQUE (pdf_id, section_ref_hash, model_id),

                CONSTRAINT fk_pdf_vl_summaries_workspace
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE
            )
        SQL);

        // Serves the _cache_hit() lookup (WHERE workspace_id/pdf_id/
        // section_ref_hash/model_id) directly, in predicate order.
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_pdf_vl_summaries_lookup
             ON silver.pdf_vl_summaries (workspace_id, pdf_id, section_ref_hash, model_id)',
        );

        DB::statement("COMMENT ON TABLE silver.pdf_vl_summaries IS
            '§04p Stage 6 — durable cache for Qwen-VL section summaries (app/services/pdf_vl.py). Backfilled into the migration chain 2026-08-15; previously existed only out-of-band (see migration docblock).'");

        DB::statement('GRANT SELECT, INSERT ON silver.pdf_vl_summaries TO georag_app');

        DB::statement('ALTER TABLE silver.pdf_vl_summaries ENABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE silver.pdf_vl_summaries FORCE ROW LEVEL SECURITY');
        DB::statement('DROP POLICY IF EXISTS '.self::POLICY.' ON silver.pdf_vl_summaries');
        DB::statement(
            'CREATE POLICY '.self::POLICY.' ON silver.pdf_vl_summaries'
            .' USING (workspace_id = NULLIF(current_setting(\'app.workspace_id\', true), \'\')::uuid)'
            .' WITH CHECK (workspace_id = NULLIF(current_setting(\'app.workspace_id\', true), \'\')::uuid)',
        );
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP TABLE IF EXISTS silver.pdf_vl_summaries CASCADE');
    }
};
