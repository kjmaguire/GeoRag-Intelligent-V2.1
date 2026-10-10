<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * audit.verify_hash_chain() no longer flags the first in-window row of every
 * chain as a break (2026-10 Hatchet audit, `audit_ledger_verify`).
 *
 * Each chain's first row inside [start, end) has a stored previous_hash that
 * is the hash of the newest row BEFORE the window. The verifier took
 * `LAG(hash) OVER (PARTITION BY workspace_id ORDER BY created_at, id)` over
 * the in-window rows only, so that first row's expected_prev was NULL, its
 * stored_prev was not, and `stored_prev IS DISTINCT FROM expected_prev` put it
 * in the result. On a clean ledger the nightly 24 h walk reported one false
 * break per workspace that already had history (reproduced on a three-chain
 * ledger: two false breaks on the second day, none on the whole range).
 * docs/phase0_handoff.md recorded it as R-P0-8 with a workaround that only the
 * acceptance script used; the scheduled verifier never did, which is also why
 * nothing could usefully alarm on `status = 'break'`.
 *
 * The first row of each chain in the window now takes its expected_prev from
 * the newest row of the SAME chain before p_start_at, found with
 * (created_at DESC, id DESC) -- the order the BEFORE INSERT trigger uses to
 * pick a parent, and in two index-friendly branches (`= workspace_id`, and
 * `IS NULL` for the system chain) for the same reason 2026_10_04_200000 split
 * the trigger's lookup: `IS NOT DISTINCT FROM` cannot use
 * audit_ledger_workspace_id_idx. A chain with nothing before the window still
 * expects NULL. Tampering with the pre-window predecessor is now caught at the
 * first in-window row rather than hidden by it.
 *
 * "First in the window" is `row_number() = 1`, not "LAG is NULL": a previous
 * in-window row whose stored hash is itself NULL must not fall back to the
 * seed.
 *
 * Same signature and RETURNS TABLE, so CREATE OR REPLACE keeps the EXECUTE
 * grants 2026_08_20_030000 made. database/raw/phase0/100-audit-verify-
 * function.sql carries the same body -- edit both together.
 *
 * down() restores the 2026_08_20_030000 body (the windowed LAG with no seed).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (! $this->canInstall()) {
            return;
        }

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
    WITH in_window AS (
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
            l.created_at
        FROM audit.audit_ledger l
        WHERE l.created_at >= p_start_at
          AND l.created_at <  p_end_at
    ),
    chains AS (
        SELECT DISTINCT w.workspace_id FROM in_window w
    ),
    -- The row each chain's first in-window row hangs off: the newest row of
    -- the SAME chain strictly before the window. Two branches, not
    -- `IS NOT DISTINCT FROM`, so each is an index probe on
    -- audit_ledger_workspace_id_idx (workspace_id, created_at DESC).
    seeds AS (
        SELECT c.workspace_id,
               COALESCE(sw.hash, sn.hash) AS seed_hash
          FROM chains c
          LEFT JOIN LATERAL (
                SELECT p.hash
                  FROM audit.audit_ledger p
                 WHERE c.workspace_id IS NOT NULL
                   AND p.workspace_id = c.workspace_id
                   AND p.created_at < p_start_at
                 ORDER BY p.created_at DESC, p.id DESC
                 LIMIT 1
          ) sw ON true
          LEFT JOIN LATERAL (
                SELECT p.hash
                  FROM audit.audit_ledger p
                 WHERE c.workspace_id IS NULL
                   AND p.workspace_id IS NULL
                   AND p.created_at < p_start_at
                 ORDER BY p.workspace_id, p.created_at DESC, p.id DESC
                 LIMIT 1
          ) sn ON true
    ),
    ordered AS (
        SELECT
            w.id,
            w.workspace_id,
            w.actor_id,
            w.actor_kind,
            w.action_type,
            w.target_schema,
            w.target_table,
            w.target_id,
            w.payload,
            w.stored_prev,
            w.stored_hash,
            w.created_at,
            CASE WHEN row_number() OVER chain = 1
                 THEN s.seed_hash
                 ELSE LAG(w.stored_hash) OVER chain
            END AS expected_prev
        FROM in_window w
        JOIN seeds s ON s.workspace_id IS NOT DISTINCT FROM w.workspace_id
        WINDOW chain AS (PARTITION BY w.workspace_id ORDER BY w.created_at, w.id)
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
    'Pure-SQL hash-chain verifier. Returns mismatched rows; empty result = chain intact. The first in-window row of each chain is checked against the newest row of that chain before the window.';
SQL);
    }

    public function down(): void
    {
        if (! $this->canInstall()) {
            return;
        }

        // 2026_08_20_030000 body, verbatim: the windowed LAG with no seed.
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
    }

    /**
     * The function reads the ledger and calls recompute_hash(), both created
     * earlier in the chain; a LANGUAGE sql body is validated at CREATE time, so
     * a cluster without them is skipped rather than failed.
     */
    private function canInstall(): bool
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return false;
        }

        return (bool) (DB::selectOne(
            "SELECT to_regclass('audit.audit_ledger') IS NOT NULL
                AND to_regprocedure('audit.recompute_hash(bytea,bigint,text,text,text,text,text,jsonb,timestamptz)') IS NOT NULL AS present",
        )->present ?? false);
    }
};
