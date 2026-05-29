-- =============================================================================
-- Phase 0 — Layer D — outbox.pending_propagations + propagation_attempts
--
-- The outbox pattern (master plan §23.7) decouples Postgres writes from
-- secondary-store writes. Every write that needs to land in Qdrant/Neo4j/
-- SeaweedFS is mirrored into pending_propagations within the same Postgres
-- transaction; the outbox_dispatcher Hatchet workflow polls and propagates.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- outbox.pending_propagations
--
-- Each row is a unit of work for the outbox dispatcher: "write source row X
-- into target store Y with payload Z." Status transitions are recorded in
-- propagation_attempts (one row per attempt).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outbox.pending_propagations (
    id                              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id                    uuid        NULL,
    source_schema                   text        NOT NULL,
    source_table                    text        NOT NULL,
    source_id                       text        NOT NULL,
    target_store                    text        NOT NULL
        CHECK (target_store IN ('qdrant','neo4j','seaweedfs','redis','external_webhook')),
    target_collection               text        NULL,                                       -- collection / database / bucket
    operation                       text        NOT NULL
        CHECK (operation IN ('upsert','delete','reindex')),
    payload                         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key                 text        NOT NULL,
    target_store_concurrency_hint   smallint    NOT NULL DEFAULT 4,
    status                          text        NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','in_flight','succeeded','dead_lettered')),
    enqueued_at                     timestamptz NOT NULL DEFAULT now(),
    last_attempted_at               timestamptz NULL,
    succeeded_at                    timestamptz NULL,
    dead_lettered_at                timestamptz NULL,
    audit_ledger_ref                uuid        NULL                                        -- the audit_ledger row that triggered this propagation
);

COMMENT ON TABLE  outbox.pending_propagations IS
    'One row per multi-store write awaiting dispatch to a secondary store (Qdrant/Neo4j/SeaweedFS).';
COMMENT ON COLUMN outbox.pending_propagations.idempotency_key IS
    'Stable key the dispatcher uses to dedupe — derived from (target_store, target_collection, source_id, operation).';
COMMENT ON COLUMN outbox.pending_propagations.target_store_concurrency_hint IS
    'Max concurrent attempts in flight against this target. Qdrant ~10, Neo4j ~4 — dispatcher reads per-row.';

-- Idempotency: same logical write should not be enqueued twice.
CREATE UNIQUE INDEX IF NOT EXISTS pending_propagations_idempotency_unique
    ON outbox.pending_propagations (target_store, idempotency_key)
    WHERE status IN ('pending','in_flight');

CREATE INDEX IF NOT EXISTS pending_propagations_dispatch_idx
    ON outbox.pending_propagations (target_store, status, enqueued_at)
    WHERE status IN ('pending','in_flight');
CREATE INDEX IF NOT EXISTS pending_propagations_workspace_idx
    ON outbox.pending_propagations (workspace_id, enqueued_at DESC) WHERE workspace_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS pending_propagations_source_idx
    ON outbox.pending_propagations (source_schema, source_table, source_id);
CREATE INDEX IF NOT EXISTS pending_propagations_dead_letter_idx
    ON outbox.pending_propagations (dead_lettered_at DESC) WHERE status = 'dead_lettered';

-- ---------------------------------------------------------------------------
-- outbox.propagation_attempts
--
-- One row per attempt. Multiple rows per propagation are normal — transient
-- failures retry up to N times, after which the propagation is dead-lettered.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outbox.propagation_attempts (
    id              bigserial   PRIMARY KEY,
    propagation_id  uuid        NOT NULL REFERENCES outbox.pending_propagations(id) ON DELETE CASCADE,
    workspace_id    uuid        NULL,
    attempt_no      smallint    NOT NULL,
    status          text        NOT NULL
        CHECK (status IN ('success','transient_failure','permanent_failure','dead_lettered')),
    error_kind      text        NULL,                                                         -- 'timeout','rate_limit','schema_mismatch','auth','other'
    error_message   text        NULL,
    error_detail    jsonb       NULL,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz NULL,
    duration_ms     bigint      GENERATED ALWAYS AS (
                        CASE WHEN finished_at IS NULL THEN NULL
                             ELSE EXTRACT(EPOCH FROM (finished_at - started_at)) * 1000 END
                    ) STORED,
    audit_ledger_ref uuid       NULL,
    CONSTRAINT propagation_attempts_propagation_attempt_no UNIQUE (propagation_id, attempt_no)
);

COMMENT ON TABLE  outbox.propagation_attempts IS
    'Per-attempt record of secondary-store writes. Same propagation_id may have several rows (retries).';

CREATE INDEX IF NOT EXISTS propagation_attempts_propagation_idx
    ON outbox.propagation_attempts (propagation_id, attempt_no);
CREATE INDEX IF NOT EXISTS propagation_attempts_status_idx
    ON outbox.propagation_attempts (status, started_at DESC);
CREATE INDEX IF NOT EXISTS propagation_attempts_workspace_idx
    ON outbox.propagation_attempts (workspace_id, started_at DESC) WHERE workspace_id IS NOT NULL;
