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
--
-- The FIRST row of each chain inside the window has a stored previous_hash that
-- is the hash of the newest row of that chain BEFORE the window. It is checked
-- against that row (the `seeds` CTE), not against NULL: taking LAG() over the
-- in-window rows alone gave the first row an expected_prev of NULL and so
-- reported one false break per chain that already had history (R-P0-8,
-- docs/phase0_handoff.md). A chain with nothing before the window still
-- expects NULL. Kept in lockstep with migration 2026_10_10_100000.
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
$$;

COMMENT ON FUNCTION audit.verify_hash_chain(timestamptz, timestamptz) IS
    'Pure-SQL hash-chain verifier. Returns mismatched rows; empty result = chain intact. The first in-window row of each chain is checked against the newest row of that chain before the window.';

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
    -- HAT-2 (2026-09-29): platform verification runs belong to the default
    -- workspace, the id 98-rls-tenant-isolation-block3.sql back-fills with.
    -- The scope is bound only around the writes; the ledger reads keep the
    -- caller's scope so verify_hash_chain walks the whole chain. Kept in
    -- lockstep with migration 2026_09_29_200000.
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
END $$;

COMMENT ON FUNCTION audit.run_verification(timestamptz, timestamptz, uuid) IS
    'End-to-end verifier: runs verify_hash_chain for the given range and writes the result row. Returns the run id.';
