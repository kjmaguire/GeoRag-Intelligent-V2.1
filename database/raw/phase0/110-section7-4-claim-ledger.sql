-- =============================================================================
-- §7.4 Claim Ledger — table + service
--
-- The audit explicitly flagged this as missing. Per master plan §7.4:
-- "Every claim the LLM makes must be entered in the claim ledger with
-- its required support type and verification status."
--
-- This is the structured complement to silver.answer_citation_items
-- (which records citations) — the ledger records the *claims* themselves
-- along with what kind of evidence is required to support each one.
-- =============================================================================

CREATE TABLE IF NOT EXISTS silver.claim_ledger (
    claim_id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id           uuid NOT NULL,
    answer_run_id          uuid NOT NULL,
    claim_text             text NOT NULL,
    claim_type             varchar(32) NOT NULL,  -- numeric | entity | temporal | spatial | relationship | refusal | qualitative
    required_support_type  varchar(32) NOT NULL,  -- citation | structured_row | computation | none
    verification_status    varchar(16) NOT NULL DEFAULT 'pending',
    verifier               varchar(64),           -- e.g. 'layer3_numeric', 'layer4_entity', 'layer5_provenance'
    verifier_evidence_json jsonb,                 -- what the verifier consulted (citation_ids, row PKs, etc.)
    confidence_score       numeric(5,3),          -- 0-1, when verifier emits one
    source_passage_id      uuid,                  -- the chunk the claim was extracted from
    sequence_in_answer     int,                    -- ordering of claims within the answer
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT claim_type_valid CHECK (
        claim_type IN (
            'numeric','entity','temporal','spatial',
            'relationship','refusal','qualitative'
        )
    ),
    CONSTRAINT claim_support_valid CHECK (
        required_support_type IN (
            'citation','structured_row','computation','none'
        )
    ),
    CONSTRAINT claim_verification_valid CHECK (
        verification_status IN (
            'pending','verified','failed','skipped','insufficient'
        )
    )
);
CREATE INDEX IF NOT EXISTS idx_claim_ledger_answer_run
    ON silver.claim_ledger (answer_run_id);
CREATE INDEX IF NOT EXISTS idx_claim_ledger_workspace
    ON silver.claim_ledger (workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_claim_ledger_status
    ON silver.claim_ledger (verification_status) WHERE verification_status != 'verified';
CREATE INDEX IF NOT EXISTS idx_claim_ledger_type
    ON silver.claim_ledger (claim_type);

-- RLS
ALTER TABLE silver.claim_ledger ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS claim_ledger_ws_isolation ON silver.claim_ledger;
CREATE POLICY claim_ledger_ws_isolation ON silver.claim_ledger
    USING (
        workspace_id = (NULLIF(current_setting('app.workspace_id', true), '')::uuid)
        OR NULLIF(current_setting('app.workspace_id', true), '') IS NULL
    )
    WITH CHECK (
        workspace_id = (NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    );

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON silver.claim_ledger TO georag_app;
    END IF;
END $$;

DO $$
BEGIN
    RAISE NOTICE '§7.4 claim_ledger: ready.';
END $$;
