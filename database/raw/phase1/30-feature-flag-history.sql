-- =============================================================================
-- Phase 1 R-P1-6 — feature-flag audit trail.
--
-- Sidecar history table + trigger. Every INSERT / UPDATE / DELETE on
-- workspace.feature_flags emits a row recording the new value, the prior
-- value, and the actor (read from `app.actor_id` GUC if the caller set
-- it; falls back to NULL — the flag's own updated_by column).
--
-- Why this matters: during the Phase 1 cutover the operator may bump
-- traffic_pct several times per day. The Step 6 dashboard shows the
-- *current* value; this table backs a future timeline view + lets the
-- runbook's "what was the value at 14:00 UTC?" question be answered
-- with a single SELECT.
--
-- Idempotent. RLS-enabled per the Phase 0 tenant_isolation pattern; the
-- platform-default rows (workspace_id NULL) are visible to admins.
-- =============================================================================

CREATE TABLE IF NOT EXISTS workspace.feature_flag_history (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    flag_id         uuid        NOT NULL,
    workspace_id    uuid        NULL,                            -- copied from the flag row; NULL = platform default
    flag_name       text        NOT NULL,
    op              text        NOT NULL CHECK (op IN ('INSERT','UPDATE','DELETE')),
    old_bool_value      boolean NULL,
    old_int_value       integer NULL,
    old_string_value    text    NULL,
    old_json_value      jsonb   NULL,
    new_bool_value      boolean NULL,
    new_int_value       integer NULL,
    new_string_value    text    NULL,
    new_json_value      jsonb   NULL,
    actor_id        bigint      NULL,                            -- from app.actor_id GUC if set
    changed_at      timestamptz NOT NULL DEFAULT clock_timestamp()
);

COMMENT ON TABLE  workspace.feature_flag_history IS
    'Phase 1 R-P1-6 — append-only audit trail for workspace.feature_flags. One row per flag mutation.';
COMMENT ON COLUMN workspace.feature_flag_history.actor_id IS
    'Read from app.actor_id GUC at trigger fire time. NULL when caller did not stamp it.';

CREATE INDEX IF NOT EXISTS feature_flag_history_flag_idx
    ON workspace.feature_flag_history (flag_name, changed_at DESC);
CREATE INDEX IF NOT EXISTS feature_flag_history_workspace_idx
    ON workspace.feature_flag_history (workspace_id, changed_at DESC);
CREATE INDEX IF NOT EXISTS feature_flag_history_changed_at_idx
    ON workspace.feature_flag_history (changed_at DESC);

ALTER TABLE workspace.feature_flag_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace.feature_flag_history FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON workspace.feature_flag_history;
CREATE POLICY tenant_isolation ON workspace.feature_flag_history
    USING (
        workspace_id IS NULL
        OR workspace_id IS NOT DISTINCT FROM
           NULLIF(current_setting('app.workspace_id', true), '')::uuid
        OR current_setting('app.workspace_id', true) IS NULL
        OR current_setting('app.workspace_id', true) = ''
    )
    WITH CHECK (true);  -- only the trigger writes; no direct INSERTs from app

GRANT SELECT, INSERT ON workspace.feature_flag_history TO georag_app;


-- ---------------------------------------------------------------------------
-- Trigger function — fires on AFTER INSERT/UPDATE/DELETE.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION workspace.feature_flags_audit() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = workspace, pg_catalog
AS $$
DECLARE
    actor_setting text := current_setting('app.actor_id', true);
    actor_bigint  bigint := NULL;
BEGIN
    -- app.actor_id may be unset (empty string) or a numeric string. Anything
    -- non-numeric maps to NULL rather than raising.
    IF actor_setting IS NOT NULL AND actor_setting <> '' THEN
        BEGIN
            actor_bigint := actor_setting::bigint;
        EXCEPTION WHEN OTHERS THEN
            actor_bigint := NULL;
        END;
    END IF;

    IF TG_OP = 'INSERT' THEN
        INSERT INTO workspace.feature_flag_history (
            flag_id, workspace_id, flag_name, op,
            new_bool_value, new_int_value, new_string_value, new_json_value,
            actor_id
        ) VALUES (
            NEW.id, NEW.workspace_id, NEW.flag_name, 'INSERT',
            NEW.bool_value, NEW.int_value, NEW.string_value, NEW.json_value,
            COALESCE(actor_bigint, NEW.updated_by)
        );
        RETURN NEW;

    ELSIF TG_OP = 'UPDATE' THEN
        -- Skip no-op UPDATEs (timestamp-only churn).
        IF NEW.bool_value   IS NOT DISTINCT FROM OLD.bool_value
           AND NEW.int_value    IS NOT DISTINCT FROM OLD.int_value
           AND NEW.string_value IS NOT DISTINCT FROM OLD.string_value
           AND NEW.json_value   IS NOT DISTINCT FROM OLD.json_value
        THEN
            RETURN NEW;
        END IF;

        INSERT INTO workspace.feature_flag_history (
            flag_id, workspace_id, flag_name, op,
            old_bool_value, old_int_value, old_string_value, old_json_value,
            new_bool_value, new_int_value, new_string_value, new_json_value,
            actor_id
        ) VALUES (
            NEW.id, NEW.workspace_id, NEW.flag_name, 'UPDATE',
            OLD.bool_value, OLD.int_value, OLD.string_value, OLD.json_value,
            NEW.bool_value, NEW.int_value, NEW.string_value, NEW.json_value,
            COALESCE(actor_bigint, NEW.updated_by)
        );
        RETURN NEW;

    ELSIF TG_OP = 'DELETE' THEN
        INSERT INTO workspace.feature_flag_history (
            flag_id, workspace_id, flag_name, op,
            old_bool_value, old_int_value, old_string_value, old_json_value,
            actor_id
        ) VALUES (
            OLD.id, OLD.workspace_id, OLD.flag_name, 'DELETE',
            OLD.bool_value, OLD.int_value, OLD.string_value, OLD.json_value,
            COALESCE(actor_bigint, OLD.updated_by)
        );
        RETURN OLD;
    END IF;

    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS feature_flags_audit_trg ON workspace.feature_flags;
CREATE TRIGGER feature_flags_audit_trg
    AFTER INSERT OR UPDATE OR DELETE ON workspace.feature_flags
    FOR EACH ROW EXECUTE FUNCTION workspace.feature_flags_audit();


-- ---------------------------------------------------------------------------
-- Verification
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    n_table   int;
    n_trigger int;
BEGIN
    SELECT count(*) INTO n_table   FROM information_schema.tables
        WHERE table_schema='workspace' AND table_name='feature_flag_history';
    SELECT count(*) INTO n_trigger FROM information_schema.triggers
        WHERE event_object_schema='workspace'
          AND event_object_table='feature_flags'
          AND trigger_name='feature_flags_audit_trg';
    RAISE NOTICE 'R-P1-6: history table=%, trigger=%', n_table, n_trigger;
END $$;
