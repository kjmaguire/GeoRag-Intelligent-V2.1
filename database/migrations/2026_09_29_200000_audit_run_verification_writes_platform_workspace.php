<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * audit.run_verification() writes its run row with the platform workspace id
 * (HAT-2, 2026-09-29 Hatchet audit, §07b audit_ledger_verify).
 *
 * The function inserted into audit.audit_ledger_verification_runs with no
 * workspace_id. 2026_05_17_120200 dropped NOT NULL to let it, and
 * database/raw/phase0/98-rls-tenant-isolation-block3.sql puts NOT NULL and a
 * strict WITH CHECK back on every deploy (manifest.json, after `migrate`). So
 * every 17:00 UTC tick of audit_ledger_verify died on a NOT NULL violation and
 * the hash-chain verifier never recorded a run. Reproduced on PostgreSQL 16
 * after `migrate` + `db:apply-raw`, as georag_app.
 *
 * Block 3 already says what a platform verification run belongs to: it
 * back-fills the historical NULL rows with the default workspace
 * a0000000-0000-0000-0000-000000000001. The function now writes that same id,
 * which satisfies both NOT NULL and the strict WITH CHECK, and changes no
 * policy, so the FORCE RLS parity baseline is untouched.
 *
 * The scope is bound only around the two writes. The ledger reads run under
 * the CALLER's scope, as before: binding the platform workspace for the whole
 * function would narrow verify_hash_chain() to one tenant's slice of the
 * ledger, and a partial walk reports false breaks.
 *
 * database/raw/phase0/100-audit-verify-function.sql carries the same body.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (! $this->tableExists('audit.audit_ledger_verification_runs')) {
            return;
        }

        DB::unprepared(<<<'SQL'
CREATE OR REPLACE FUNCTION audit.run_verification(
    p_start_at timestamptz,
    p_end_at   timestamptz,
    p_workflow_run_id uuid DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql AS $fn$
DECLARE
    -- Platform verification runs belong to the default workspace, the same
    -- id phase0/98 back-fills historical rows with.
    c_platform_workspace CONSTANT uuid := 'a0000000-0000-0000-0000-000000000001';
    v_caller_scope text := COALESCE(current_setting('app.workspace_id', true), '');
    v_run_id uuid := gen_random_uuid();
    v_rows_total bigint;
    v_breaks bigint;
    v_first_id uuid;
    v_last_id uuid;
    v_first_hash bytea;
    v_last_hash bytea;
    v_broken_ids uuid[];
BEGIN
    PERFORM set_config('app.workspace_id', c_platform_workspace::text, true);
    INSERT INTO audit.audit_ledger_verification_runs
        (id, workspace_id, partition_date, status, started_at, workflow_run_id)
    VALUES (v_run_id, c_platform_workspace, p_start_at::date, 'in_progress',
            now(), p_workflow_run_id);
    PERFORM set_config('app.workspace_id', v_caller_scope, true);

    -- Postgres has no min/max aggregate for uuid, so use scalar subqueries.
    SELECT count(*) INTO v_rows_total
      FROM audit.audit_ledger
     WHERE created_at >= p_start_at AND created_at < p_end_at;

    SELECT id, hash INTO v_first_id, v_first_hash
      FROM audit.audit_ledger
     WHERE created_at >= p_start_at AND created_at < p_end_at
     ORDER BY created_at, id LIMIT 1;

    SELECT id, hash INTO v_last_id, v_last_hash
      FROM audit.audit_ledger
     WHERE created_at >= p_start_at AND created_at < p_end_at
     ORDER BY created_at DESC, id DESC LIMIT 1;

    SELECT array_agg(audit_id), count(*)
      INTO v_broken_ids, v_breaks
    FROM audit.verify_hash_chain(p_start_at, p_end_at);

    PERFORM set_config('app.workspace_id', c_platform_workspace::text, true);
    UPDATE audit.audit_ledger_verification_runs
       SET status        = CASE WHEN v_breaks = 0 THEN 'clean' ELSE 'break' END,
           rows_verified = COALESCE(v_rows_total, 0),
           first_id      = v_first_id,
           last_id       = v_last_id,
           first_hash    = v_first_hash,
           last_hash     = v_last_hash,
           broken_ids    = v_broken_ids,
           completed_at  = now()
     WHERE id = v_run_id;
    PERFORM set_config('app.workspace_id', v_caller_scope, true);

    RETURN v_run_id;
END $fn$;
SQL);

        DB::unprepared(<<<'SQL'
COMMENT ON FUNCTION audit.run_verification(timestamptz, timestamptz, uuid) IS
    'End-to-end verifier: runs verify_hash_chain for the given range under the caller''s scope and writes the result row under the platform workspace. Returns the run id.';
SQL);
    }

    /**
     * Nothing to undo. The previous body fails NOT NULL on every database
     * that has run `db:apply-raw`, so restoring it would restore the outage.
     */
    public function down(): void {}

    /**
     * `to_regclass` returns NULL rather than raising for an absent relation.
     */
    private function tableExists(string $qualified): bool
    {
        return DB::selectOne(
            'SELECT to_regclass(?) IS NOT NULL AS present',
            [$qualified],
        )?->present ?? false;
    }
};
