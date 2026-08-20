<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Repairs `audit.audit_ledger_verification_runs` and brings the three audit
 * verification functions under version control.
 *
 * The nightly `audit_ledger_verify` workflow (cron `0 2 * * *`) has failed on
 * every run observed in Log Analytics — 2026-08-11 through 2026-08-20 — with:
 *
 *   asyncpg.exceptions.UndefinedColumnError: column "workflow_run_id" of
 *   relation "audit_ledger_verification_runs" does not exist
 *
 * So the audit-ledger hash chain has never actually been verified on Azure.
 *
 * ── Why the table is the wrong shape ────────────────────────────────────────
 *
 * Two definitions of this table exist:
 *
 *   canonical  database/raw/phase0/20-layer-b-audit-ledger.sql   14 columns
 *   mirror     2026_05_14_140000_provision_audit_schema_for_test_db.php  8 columns
 *
 * The mirror is deliberately minimal and says so in its own docblock:
 *
 *   "CREATE TABLE IF NOT EXISTS is a no-op on production where the
 *    partitioned parent already exists"
 *
 * That is true of a cluster where `database/raw/` was applied first. It is
 * false of a cluster built from the migration chain alone, which is every
 * Azure cluster — CD runs `laravel-migrate-job` and nothing else. There the
 * mirror runs first, succeeds, and *becomes* the production schema. The two
 * shapes were also never actually "the same column shape" as the mirror
 * claims: it is missing six columns and renamed a seventh.
 *
 * ── What this migration changes ─────────────────────────────────────────────
 *
 *   1. adds the six columns `audit.run_verification()` writes but the mirror
 *      never created: workflow_run_id, first_id, last_id, first_hash,
 *      last_hash, broken_ids
 *   2. adds `error_message` (canonical) alongside the mirror's `error_text`.
 *      Both are kept — dropping `error_text` would discard rows on a cluster
 *      that has been writing to it, and it costs one nullable column to let
 *      the two converge without data loss.
 *   3. widens the status CHECK to the union of both vocabularies. The mirror
 *      allows 'pending'; the canonical function writes 'in_progress'. Fixing
 *      only the columns would have swapped an UndefinedColumn error for a
 *      CheckViolation on the very same INSERT.
 *   4. installs audit.recompute_hash / verify_hash_chain / run_verification
 *      verbatim from database/raw/phase0/100-audit-verify-function.sql.
 *
 * Point 4 is the part that makes this durable. The function chain
 * run_verification -> verify_hash_chain -> recompute_hash is declared ONLY in
 * that raw file, so nothing guarantees the version on any given server matches
 * the table this migration just fixed. `CREATE OR REPLACE` here pins both
 * halves of the contract in the same migration. If you edit the raw file,
 * edit this migration too, or delete the raw file — same rule as
 * 2026_08_19_040000_install_workflow_functions_from_raw_sql.
 *
 * Idempotent throughout: ADD COLUMN IF NOT EXISTS, DROP CONSTRAINT IF EXISTS,
 * CREATE OR REPLACE FUNCTION.
 */
return new class extends Migration
{
    /** Columns the canonical table has that the test-DB mirror omits. */
    private const MISSING_COLUMNS = [
        'workflow_run_id' => 'uuid',
        'first_id' => 'uuid',
        'last_id' => 'uuid',
        'first_hash' => 'bytea',
        'last_hash' => 'bytea',
        'broken_ids' => 'uuid[]',
        'error_message' => 'text',
    ];

    /**
     * Union of the canonical vocabulary ('in_progress') and the mirror's
     * ('pending'). Both are legitimate in-flight markers; a cluster that ran
     * the mirror may already hold 'pending' rows, so narrowing to canonical
     * would fail the constraint on existing data.
     */
    private const STATUS_VALUES = "'in_progress', 'pending', 'clean', 'break', 'error'";

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (! $this->tableExists('audit', 'audit_ledger_verification_runs')) {
            // The table is created earlier in the chain. If it is absent the
            // install is broken upstream and papering over it here would only
            // move the failure.
            return;
        }

        foreach (self::MISSING_COLUMNS as $column => $type) {
            DB::statement(sprintf(
                'ALTER TABLE audit.audit_ledger_verification_runs ADD COLUMN IF NOT EXISTS %s %s NULL',
                $column,
                $type,
            ));
        }

        // Carry any text the mirror already collected across to the canonical
        // column, so a reader of error_message sees the full history rather
        // than a cliff at this migration.
        if ($this->columnExists('audit', 'audit_ledger_verification_runs', 'error_text')) {
            DB::statement(
                'UPDATE audit.audit_ledger_verification_runs
                    SET error_message = error_text
                  WHERE error_message IS NULL
                    AND error_text IS NOT NULL',
            );
        }

        // The constraint is unnamed in both definitions, so Postgres derives
        // the same name for each — drop by that name and re-add the union.
        DB::statement(
            'ALTER TABLE audit.audit_ledger_verification_runs
               DROP CONSTRAINT IF EXISTS audit_ledger_verification_runs_status_check',
        );
        DB::statement(sprintf(
            'ALTER TABLE audit.audit_ledger_verification_runs
               ADD CONSTRAINT audit_ledger_verification_runs_status_check
               CHECK (status IN (%s))',
            self::STATUS_VALUES,
        ));

        DB::statement(
            "COMMENT ON COLUMN audit.audit_ledger_verification_runs.error_text IS
             'Superseded by error_message (2026_08_20_030000). Retained so historical rows are not lost; new writes go to error_message.'",
        );

        $this->installVerificationFunctions();
    }

    /**
     * Deliberately narrow.
     *
     * Dropping the columns again would break `audit.run_verification()`, which
     * is the whole point of adding them. The status CHECK is restored to the
     * canonical vocabulary rather than the mirror's, because that is what the
     * function writes.
     */
    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (! $this->tableExists('audit', 'audit_ledger_verification_runs')) {
            return;
        }

        DB::statement(
            'ALTER TABLE audit.audit_ledger_verification_runs
               DROP CONSTRAINT IF EXISTS audit_ledger_verification_runs_status_check',
        );
        DB::statement(
            "ALTER TABLE audit.audit_ledger_verification_runs
               ADD CONSTRAINT audit_ledger_verification_runs_status_check
               CHECK (status IN ('in_progress', 'clean', 'break', 'error'))",
        );
    }

    /**
     * Verbatim from database/raw/phase0/100-audit-verify-function.sql.
     *
     * Order matters: recompute_hash is called by verify_hash_chain, which is
     * called by run_verification. The first two are LANGUAGE sql and ARE
     * validated at CREATE time under the default check_function_bodies, so a
     * missing dependency fails loudly here rather than at 02:00 UTC.
     */
    private function installVerificationFunctions(): void
    {
        // Mirror of the audit.compute_audit_hash() trigger — the two must stay
        // in lockstep or verification reports false breaks on every row.
        DB::unprepared(<<<'SQL'
CREATE OR REPLACE FUNCTION audit.recompute_hash(
    p_previous_hash bytea,
    p_actor_id      bigint,
    p_actor_kind    text,
    p_action_type   text,
    p_target_schema text,
    p_target_table  text,
    p_target_id     text,
    p_payload       jsonb,
    p_created_at    timestamptz
) RETURNS bytea
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $fn$
    -- Schema-qualified digest() — see 90-audit-hash-chain-trigger.sql for why.
    SELECT public.digest(
        COALESCE(encode(p_previous_hash, 'hex'), '')
            || '|' || COALESCE(p_actor_id::text, '')
            || '|' || COALESCE(p_actor_kind, '')
            || '|' || p_action_type
            || '|' || COALESCE(p_target_schema, '')
            || '|' || COALESCE(p_target_table, '')
            || '|' || COALESCE(p_target_id, '')
            || '|' || p_payload::text
            || '|' || to_char(p_created_at AT TIME ZONE 'UTC',
                              'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        'sha256'
    );
$fn$;
SQL);

        DB::unprepared(<<<'SQL'
COMMENT ON FUNCTION audit.recompute_hash(bytea,bigint,text,text,text,text,text,jsonb,timestamptz) IS
    'Pure-SQL mirror of audit.compute_audit_hash trigger. Used by audit.verify_hash_chain.';
SQL);

        DB::unprepared(<<<'SQL'
CREATE OR REPLACE FUNCTION audit.verify_hash_chain(
    p_start_at timestamptz,
    p_end_at   timestamptz
)
RETURNS TABLE (
    audit_id        uuid,
    workspace_id    uuid,
    created_at      timestamptz,
    stored_hash     bytea,
    expected_hash   bytea,
    stored_prev     bytea,
    expected_prev   bytea
)
LANGUAGE sql STABLE PARALLEL SAFE AS $fn$
    WITH ordered AS (
        SELECT
            l.id,
            l.workspace_id,
            l.actor_id,
            l.actor_kind,
            l.action_type,
            l.target_schema,
            l.target_table,
            l.target_id,
            l.payload,
            l.previous_hash AS stored_prev,
            l.hash AS stored_hash,
            l.created_at,
            LAG(l.hash) OVER (
                PARTITION BY l.workspace_id
                ORDER BY l.created_at, l.id
            ) AS expected_prev
        FROM audit.audit_ledger l
        WHERE l.created_at >= p_start_at
          AND l.created_at <  p_end_at
    ),
    checked AS (
        SELECT
            o.id,
            o.workspace_id,
            o.created_at,
            o.stored_hash,
            audit.recompute_hash(
                o.expected_prev,
                o.actor_id, o.actor_kind, o.action_type,
                o.target_schema, o.target_table, o.target_id,
                o.payload, o.created_at
            ) AS expected_hash,
            o.stored_prev,
            o.expected_prev
        FROM ordered o
    )
    SELECT id, workspace_id, created_at, stored_hash, expected_hash,
           stored_prev, expected_prev
    FROM checked
    WHERE stored_hash IS DISTINCT FROM expected_hash
       OR stored_prev IS DISTINCT FROM expected_prev;
$fn$;
SQL);

        DB::unprepared(<<<'SQL'
COMMENT ON FUNCTION audit.verify_hash_chain(timestamptz, timestamptz) IS
    'Pure-SQL hash-chain verifier. Returns mismatched rows; empty result = chain intact.';
SQL);

        DB::unprepared(<<<'SQL'
CREATE OR REPLACE FUNCTION audit.run_verification(
    p_start_at timestamptz,
    p_end_at   timestamptz,
    p_workflow_run_id uuid DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql AS $fn$
DECLARE
    v_run_id uuid := gen_random_uuid();
    v_rows_total bigint;
    v_breaks bigint;
    v_first_id uuid;
    v_last_id uuid;
    v_first_hash bytea;
    v_last_hash bytea;
    v_broken_ids uuid[];
BEGIN
    INSERT INTO audit.audit_ledger_verification_runs
        (id, partition_date, status, started_at, workflow_run_id)
    VALUES (v_run_id, p_start_at::date, 'in_progress', now(), p_workflow_run_id);

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

    RETURN v_run_id;
END $fn$;
SQL);

        DB::unprepared(<<<'SQL'
COMMENT ON FUNCTION audit.run_verification(timestamptz, timestamptz, uuid) IS
    'End-to-end verifier: runs verify_hash_chain for the given range and writes the result row. Returns the run id.';
SQL);

        // database/raw/phase1/10-georag-app-role.sql grants EXECUTE on
        // recompute_hash to georag_app. That file is raw-only, so Azure never
        // got it, and the Hatchet worker connects as georag_app. Granting all
        // three keeps the whole call chain reachable by the role that actually
        // invokes it.
        if (! $this->roleExists('georag_app')) {
            return;
        }

        foreach ([
            'audit.recompute_hash(bytea, bigint, text, text, text, text, text, jsonb, timestamptz)',
            'audit.verify_hash_chain(timestamptz, timestamptz)',
            'audit.run_verification(timestamptz, timestamptz, uuid)',
        ] as $signature) {
            DB::statement(sprintf('GRANT EXECUTE ON FUNCTION %s TO georag_app', $signature));
        }
    }

    private function tableExists(string $schema, string $table): bool
    {
        return DB::selectOne(
            'SELECT 1 AS present
               FROM information_schema.tables
              WHERE table_schema = ? AND table_name = ?',
            [$schema, $table],
        ) !== null;
    }

    private function columnExists(string $schema, string $table, string $column): bool
    {
        return DB::selectOne(
            'SELECT 1 AS present
               FROM information_schema.columns
              WHERE table_schema = ? AND table_name = ? AND column_name = ?',
            [$schema, $table, $column],
        ) !== null;
    }

    private function roleExists(string $role): bool
    {
        return DB::selectOne(
            'SELECT 1 AS present FROM pg_roles WHERE rolname = ?',
            [$role],
        ) !== null;
    }
};
