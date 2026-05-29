-- =============================================================================
-- Phase 0 — audit_ledger hash-chain verification
--
-- Pure-SQL verifier: given a date range, walks every audit_ledger row in
-- (workspace_id, created_at, id) order, recomputes the expected hash from
-- the same recipe used by the BEFORE-INSERT trigger, and reports any rows
-- where the stored hash does not match.
--
-- The Hatchet workflow `audit_ledger_verify` is a thin scheduler that:
--   1. Calls audit.run_verification(prev_day_start, prev_day_end)
--   2. Writes the result into audit.audit_ledger_verification_runs
--
-- Pure SQL means an external auditor — running psql with the right
-- credentials — can run the same verification independently of GeoRAG code.
-- That's the point of a hash chain.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- audit.recompute_hash(audit_ledger row + previous_hash) → bytea
-- Mirror of audit.compute_audit_hash() trigger (90-audit-hash-chain-trigger.sql).
-- Stays in lockstep with the trigger: any change to one needs the same change
-- to the other (a regression test in the smoke harness pins this).
-- ---------------------------------------------------------------------------
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
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
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
$$;

COMMENT ON FUNCTION audit.recompute_hash(bytea,bigint,text,text,text,text,text,jsonb,timestamptz) IS
    'Pure-SQL mirror of audit.compute_audit_hash trigger. Used by audit.verify_hash_chain.';

-- ---------------------------------------------------------------------------
-- audit.verify_hash_chain(start_at, end_at)
--
-- Returns one row per audit_ledger row in [start_at, end_at) whose stored
-- hash does NOT match recomputation. An empty result set means the chain is
-- intact for that range.
--
-- The workspace_id grouping matters: each (workspace_id) chain is verified
-- independently, mirroring the trigger's IS NOT DISTINCT FROM scoping.
-- ---------------------------------------------------------------------------
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
LANGUAGE sql STABLE PARALLEL SAFE AS $$
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
$$;

COMMENT ON FUNCTION audit.verify_hash_chain(timestamptz, timestamptz) IS
    'Pure-SQL hash-chain verifier. Returns mismatched rows; empty result = chain intact.';

-- ---------------------------------------------------------------------------
-- audit.run_verification(start_at, end_at)
--
-- Wraps verify_hash_chain + writes the result into
-- audit.audit_ledger_verification_runs. This is what the Hatchet scheduler
-- workflow calls each night.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit.run_verification(
    p_start_at timestamptz,
    p_end_at   timestamptz,
    p_workflow_run_id uuid DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql AS $$
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
END $$;

COMMENT ON FUNCTION audit.run_verification(timestamptz, timestamptz, uuid) IS
    'End-to-end verifier: runs verify_hash_chain for the given range and writes the result row. Returns the run id.';
