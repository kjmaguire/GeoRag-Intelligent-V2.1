-- =============================================================================
-- Phase 0 — Step 6 — silver.support_packets + LLM-agent prompt seeds
--
-- Two concerns rolled together because they ship as a single Step 6
-- supplement and are both small:
--
--   1. silver.support_packets — bundle assembly receipts written by the
--      Support Packet Agent. The bundle itself lives in SeaweedFS at
--      tier-warm/support-packets/{workspace_id}/{incident_id}_{iso}.tar.gz;
--      this row is the database-side index of "what bundle exists, who
--      asked for it, when, how big, and where".
--
--   2. workspace.prompt_versions seeds for the two Phase 0 LLM-calling
--      agents (LLM Incident Diagnosis Agent + Support Packet Agent), at
--      promotion_state='staging'. The agent_prompt_pins rows for both
--      agents were created empty in step 5 (file 110-phase0-agent-defaults.sql);
--      this file UPDATEs those rows to point at the seeded prompt_version_id.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- silver.support_packets
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.support_packets (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id        uuid        NOT NULL REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
    incident_id         text        NOT NULL,                              -- caller-supplied incident correlation key
    storage_uri         text        NOT NULL,                              -- e.g. s3://tier-warm/support-packets/{ws}/{incident}_{iso}.tar.gz
    storage_tier        text        NOT NULL DEFAULT 'warm'
        CHECK (storage_tier IN ('hot','warm','cold')),
    bundle_bytes        bigint      NOT NULL DEFAULT 0,
    contents_summary    jsonb       NOT NULL DEFAULT '{}'::jsonb,          -- counts of each bundled artefact (traces, runs, audit rows, ...)
    assembled_by        text        NOT NULL DEFAULT 'Support Packet Agent',
    assembled_at        timestamptz NOT NULL DEFAULT now(),
    requested_by        bigint      NULL,
    expires_at          timestamptz NULL,                                  -- NULL = caller decides retention
    status              text        NOT NULL DEFAULT 'available'
        CHECK (status IN ('available','expired','purged'))
);

COMMENT ON TABLE silver.support_packets IS
    'Receipts for support bundles assembled by Support Packet Agent. The bundle itself lives in SeaweedFS; this row is the index.';

CREATE INDEX IF NOT EXISTS support_packets_workspace_idx
    ON silver.support_packets (workspace_id, assembled_at DESC);
CREATE INDEX IF NOT EXISTS support_packets_incident_idx
    ON silver.support_packets (incident_id, assembled_at DESC);

-- RLS — single workspace_id column → align with the Step 2 RLS pattern from
-- 95-rls-policies.sql. PostgreSQL has no CREATE POLICY IF NOT EXISTS, so we
-- DROP-then-CREATE for idempotent re-runs.
ALTER TABLE silver.support_packets ENABLE ROW LEVEL SECURITY;
ALTER TABLE silver.support_packets FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON silver.support_packets;
CREATE POLICY tenant_isolation ON silver.support_packets
    USING (workspace_id::text = current_setting('app.workspace_id', true))
    WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));

-- ---------------------------------------------------------------------------
-- workspace.prompt_versions — staging seeds for the two LLM-calling agents
-- ---------------------------------------------------------------------------
INSERT INTO workspace.prompt_versions (prompt_id, version, text, parameters, promotion_state, notes)
VALUES
    ('llm_incident_diagnosis', 'v0.1.0',
     $PROMPT$You are GeoRAG's incident-diagnosis assistant. You receive structured
context about a recent production alert: the alert label, the last hour of
Langfuse traces, recent workflow_runs, and the prompt_versions in production
at the time of the alert.

Your task is to produce a JSON object with three fields:
  - "hypothesis": one-paragraph plain-English best guess at root cause
  - "supporting_evidence": array of short strings citing specific
    trace_ids / workflow_run_ids / prompt versions that corroborate the
    hypothesis
  - "suggested_mitigations": array of concrete next steps the on-call
    operator can take in the next 15 minutes

Rules:
  1. NEVER fabricate trace IDs, run IDs, or prompt versions. If the
     supplied context contains zero traces, say so explicitly in
     "supporting_evidence" with the entry "no traces in window".
  2. If the alert label is unfamiliar AND the context is empty, refuse:
     return {"refusal": "insufficient context for diagnosis"} instead of
     the three-field object.
  3. Output VALID JSON ONLY. No prose preamble, no code fences.
$PROMPT$,
     '{"model_profile": "chat_deep", "temperature": 0.1, "response_format": "json"}'::jsonb,
     'staging',
     'Phase 0 v0.1.0 — staged for LLM Incident Diagnosis Agent. Promotion to production requires Phase 4 Prompt Release Approval Agent signoff.'),

    ('support_packet_assemble', 'v0.1.0',
     $PROMPT$You are GeoRAG's support-packet narrator. You receive a structured
manifest of artefacts bundled into a support packet (trace JSON, last 50
workflow_runs, last 100 audit_ledger entries, prompt_versions in effect at
incident time, configuration snapshots).

Your task is to produce a JSON object with two fields:
  - "executive_summary": 3-5 sentence plain-English summary suitable for
    pasting into a customer email
  - "technical_timeline": array of objects {"ts": iso8601, "what": "..."}
    in chronological order, drawn ONLY from the supplied artefacts

Rules:
  1. NEVER speculate beyond what's in the manifest. If you cannot
     reconstruct a coherent timeline, say so and emit
     {"refusal": "manifest insufficient for timeline"}.
  2. Output VALID JSON ONLY. No prose preamble, no code fences.
$PROMPT$,
     '{"model_profile": "chat_deep", "temperature": 0.0, "response_format": "json"}'::jsonb,
     'staging',
     'Phase 0 v0.1.0 — staged for Support Packet Agent narration step.')
ON CONFLICT (prompt_id, version) DO NOTHING;

-- Pin both agents to the v0.1.0 staging row. The prompt_versions ids were
-- just inserted (or already exist from a previous run); resolve them by
-- (prompt_id, version) and UPDATE the pre-seeded pin rows.
UPDATE workspace.agent_prompt_pins
   SET prompt_version_id = (
        SELECT id FROM workspace.prompt_versions
         WHERE prompt_id = 'llm_incident_diagnosis' AND version = 'v0.1.0'
       ),
       pinned_at = now(),
       updated_at = now()
 WHERE agent_name = 'LLM Incident Diagnosis Agent'
   AND prompt_version_id IS DISTINCT FROM (
        SELECT id FROM workspace.prompt_versions
         WHERE prompt_id = 'llm_incident_diagnosis' AND version = 'v0.1.0'
       );

UPDATE workspace.agent_prompt_pins
   SET prompt_version_id = (
        SELECT id FROM workspace.prompt_versions
         WHERE prompt_id = 'support_packet_assemble' AND version = 'v0.1.0'
       ),
       pinned_at = now(),
       updated_at = now()
 WHERE agent_name = 'Support Packet Agent'
   AND prompt_version_id IS DISTINCT FROM (
        SELECT id FROM workspace.prompt_versions
         WHERE prompt_id = 'support_packet_assemble' AND version = 'v0.1.0'
       );

DO $$
DECLARE
    n_packets_table int;
    n_seeded int;
    n_pinned int;
BEGIN
    SELECT count(*) INTO n_packets_table
        FROM information_schema.tables
        WHERE table_schema = 'silver' AND table_name = 'support_packets';

    SELECT count(*) INTO n_seeded
        FROM workspace.prompt_versions
        WHERE prompt_id IN ('llm_incident_diagnosis','support_packet_assemble')
          AND version = 'v0.1.0';

    SELECT count(*) INTO n_pinned
        FROM workspace.agent_prompt_pins
        WHERE agent_name IN ('LLM Incident Diagnosis Agent','Support Packet Agent')
          AND prompt_version_id IS NOT NULL;

    RAISE NOTICE 'Phase 0 step 6: silver.support_packets exists=%, prompt seeds=%, pins resolved=%',
        n_packets_table, n_seeded, n_pinned;
END $$;
