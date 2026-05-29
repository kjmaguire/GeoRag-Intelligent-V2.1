-- =============================================================================
-- Phase 0 — default agent_timeouts + agent_prompt_pins seeds (kickoff §5.3)
--
-- Seeds the 11 Phase 0 agents (corrected roster per kickoff §Step 7) with
-- the default policy: soft=30s, hard=120s, retries=1, circuit_scope=workspace,
-- threshold=5, cool_down=300s.
--
-- Idempotent — uses ON CONFLICT DO NOTHING. Re-running is safe; tuning later
-- happens via /admin/agent-config UI (deferred to Phase 4).
-- =============================================================================

INSERT INTO workspace.agent_timeouts
    (agent_name, risk_tier, soft_timeout_ms, hard_timeout_ms,
     retry_count, circuit_breaker_scope, failure_threshold, cool_down_seconds)
VALUES
    ('Tenant Isolation Auditor',        'R0', 30000, 120000, 1, 'global',    5, 300),
    ('Lineage Reporter Agent',          'R0', 10000,  30000, 1, 'workspace', 5, 300),
    ('Storage Tiering Agent',           'R2', 60000, 600000, 1, 'workspace', 3, 600),
    ('Index Health Agent',              'R0', 30000, 120000, 1, 'workspace', 5, 300),
    ('Store Reconciliation Agent',      'R0', 60000, 600000, 1, 'workspace', 3, 600),
    ('Model Upgrade Watch Agent',       'R0', 10000,  60000, 1, 'global',    5, 300),
    ('vLLM Security Check Agent',       'R0', 10000,  60000, 1, 'global',    5, 300),
    ('GPU/VRAM Health Agent',           'R0',  5000,  30000, 1, 'global',    5, 300),
    ('Model Cost Summary Agent',        'R0', 30000, 120000, 1, 'workspace', 5, 300),
    ('LLM Incident Diagnosis Agent',    'R0', 30000, 180000, 1, 'global',    3, 600),
    ('Support Packet Agent',            'R2', 60000, 300000, 1, 'workspace', 3, 600)
ON CONFLICT (agent_name) DO NOTHING;

-- ---------------------------------------------------------------------------
-- agent_prompt_pins: only LLM-calling Phase 0 agents need a pin row.
-- LLM Incident Diagnosis Agent + Support Packet Agent are the two LLM
-- callers in Phase 0. The actual prompt_versions rows aren't created yet
-- (they ship when the agent code lands in Step 6); the pin row is created
-- empty so Step 6 can UPDATE it without an INSERT path.
-- ---------------------------------------------------------------------------
INSERT INTO workspace.agent_prompt_pins (agent_name, prompt_id, prompt_version_id)
VALUES
    ('LLM Incident Diagnosis Agent', 'llm_incident_diagnosis', NULL),
    ('Support Packet Agent',         'support_packet_assemble', NULL)
ON CONFLICT (agent_name) DO NOTHING;

DO $$
DECLARE
    n_timeouts int;
    n_pins int;
BEGIN
    SELECT count(*) INTO n_timeouts FROM workspace.agent_timeouts;
    SELECT count(*) INTO n_pins FROM workspace.agent_prompt_pins;
    RAISE NOTICE 'Phase 0 agent defaults: % timeouts, % prompt pins', n_timeouts, n_pins;
END $$;
