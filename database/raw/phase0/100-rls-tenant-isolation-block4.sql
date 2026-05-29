-- =============================================================================
-- §11.5 Tenant Isolation — Block 4 closeout (Phase H4 follow-up).
--
-- Blocks 1-3 closed the active leak primitives + got every tenant table
-- workspace-scoped under strict RLS. Block 4 is the housekeeping pass:
--
--   1. Add the silver.workspaces FK constraint to the ~99 tables that
--      have a workspace_id column but were missing the FK. These were
--      mostly the Tier-B "RLS-only" tables from Block 2 + various older
--      silver tables that pre-dated the Block-1 convention.
--
--   2. public_geo.* is EXEMPT by design (Crown-copyright open
--      data, not tenant-scoped). The auditor schema list was updated to
--      remove it; no SQL changes needed in that schema.
--
-- Partitioned-table note: declaring the FK on the partitioned parent
-- (audit.audit_ledger, workflow.workflow_runs) propagates to all
-- existing + future partitions in PG 13+, so the audit_ledger_p* and
-- workflow_runs_p* children only need the parent's FK.
--
-- Idempotent: each ADD CONSTRAINT is guarded by a NOT EXISTS check.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    spec record;
    fqtn text;
    cn   text;
BEGIN
    FOR spec IN
        SELECT s, t FROM (VALUES
            -- audit (partitioned parents inherit to children)
            ('audit', 'audit_ledger'),
            ('audit', 'audit_ledger_verification_runs'),
            ('audit', 'integration_credentials_audit'),
            -- gold visual tables
            ('gold',  'cross_section_panels'),
            ('gold',  'drillhole_intervals_visual'),
            ('gold',  'structure_measurements_visual'),
            -- ops support cockpit
            ('ops',   'support_replay_runs'),
            ('ops',   'support_ticket_traces'),
            ('ops',   'support_tickets'),
            -- silver (large list)
            ('silver', 'agent_conversation_messages'),
            ('silver', 'agent_conversations'),
            ('silver', 'alterations'),
            ('silver', 'answer_citation_items'),
            ('silver', 'answer_citation_spans'),
            ('silver', 'answer_retrieval_items'),
            ('silver', 'answer_runs'),
            ('silver', 'assay_events'),
            ('silver', 'assay_results'),
            ('silver', 'collaboration_audit_log'),
            ('silver', 'collaboration_comments'),
            ('silver', 'collaboration_mentions'),
            ('silver', 'collaboration_review_requests'),
            ('silver', 'collars'),
            ('silver', 'corpus_health_findings'),
            ('silver', 'decision_evidence_links'),
            ('silver', 'decision_lessons_learned'),
            ('silver', 'decision_options'),
            ('silver', 'decision_outcomes'),
            ('silver', 'decision_records'),
            ('silver', 'document_ingestion_quality'),
            ('silver', 'document_passages'),
            ('silver', 'document_revisions'),
            ('silver', 'drill_traces'),
            ('silver', 'evidence_items'),
            ('silver', 'exports'),
            ('silver', 'geochemistry'),
            ('silver', 'geological_formations'),
            ('silver', 'historic_workings'),
            ('silver', 'hypotheses'),
            ('silver', 'hypothesis_evidence_links'),
            ('silver', 'ingest_extractions'),
            ('silver', 'ingest_layouts'),
            ('silver', 'ingest_ocr_results'),
            ('silver', 'kg_formation_aliases'),
            ('silver', 'kg_mineral_aliases'),
            ('silver', 'kg_report_aliases'),
            ('silver', 'kg_sample_aliases'),
            ('silver', 'lithology_logs'),
            ('silver', 'low_confidence_page_reviews'),
            ('silver', 'message_feedback'),
            ('silver', 'mineral_claims'),
            ('silver', 'ocr_page_quality'),
            ('silver', 'parser_run_artifacts'),
            ('silver', 'pdf_coordinates'),
            ('silver', 'pdf_layout_regions'),
            ('silver', 'pdf_ocr_results'),
            ('silver', 'pdf_table_cells'),
            ('silver', 'pdf_text_blocks'),
            ('silver', 'pdf_vl_summaries'),
            ('silver', 'project_boundaries'),
            ('silver', 'projects'),
            ('silver', 'raster_layers'),
            ('silver', 'reports'),
            ('silver', 'review_audit_log'),
            ('silver', 'review_queue'),
            ('silver', 'samples'),
            ('silver', 'saved_map_views'),
            ('silver', 'seismic_surveys'),
            ('silver', 'source_trust_features'),
            ('silver', 'source_trust_scores'),
            ('silver', 'spatial_features'),
            ('silver', 'storage_tier_policy'),
            ('silver', 'store_reconciliation_findings'),
            ('silver', 'structured_record_lineage'),
            ('silver', 'structures'),
            ('silver', 'support_packets'),
            ('silver', 'surveys'),
            ('silver', 'table_extraction_quality'),
            ('silver', 'well_log_curves'),
            -- targeting
            ('targeting', 'target_backtests'),
            ('targeting', 'target_candidate_zones'),
            ('targeting', 'target_outcomes'),
            ('targeting', 'target_recommendations'),
            ('targeting', 'target_review_decisions'),
            ('targeting', 'target_score_factors'),
            ('targeting', 'target_scores'),
            ('targeting', 'target_uncertainties'),
            -- workflow (partitioned parent propagates to children)
            ('workflow', 'workflow_run_events'),
            ('workflow', 'workflow_runs')
        ) AS v(s, t)
    LOOP
        cn := spec.t || '_workspace_id_fkey';
        fqtn := spec.s || '.' || spec.t;

        -- Skip if FK already exists.
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.table_constraints
             WHERE table_schema = spec.s
               AND table_name   = spec.t
               AND constraint_name = cn
        )
        -- Also skip if the table inherits an FK from a partitioned
        -- parent (PG auto-creates child FKs in this case).
        AND NOT EXISTS (
            SELECT 1 FROM pg_inherits i
             WHERE i.inhrelid = (spec.s || '.' || spec.t)::regclass
        )
        THEN
            BEGIN
                -- First try strict FK (matches Block 1-3 pattern).
                EXECUTE format(
                    'ALTER TABLE %I.%I '
                    'ADD CONSTRAINT %I FOREIGN KEY (workspace_id) '
                    'REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE',
                    spec.s, spec.t, cn
                );
            EXCEPTION
                WHEN foreign_key_violation THEN
                    -- Some tables (audit.audit_ledger has 4k+ rows
                    -- pointing at deleted test workspaces) can't take
                    -- a strict FK without destroying history. Fall
                    -- back to NOT VALID: blocks NEW orphan rows while
                    -- preserving existing audit history. Run
                    -- VALIDATE CONSTRAINT after a separate cleanup.
                    EXECUTE format(
                        'ALTER TABLE %I.%I '
                        'ADD CONSTRAINT %I FOREIGN KEY (workspace_id) '
                        'REFERENCES silver.workspaces(workspace_id) '
                        'ON DELETE CASCADE NOT VALID',
                        spec.s, spec.t, cn
                    );
                    RAISE NOTICE 'Added %.% FK as NOT VALID — orphan workspace_ids '
                                 'in existing rows. VALIDATE CONSTRAINT after cleanup.',
                                 spec.s, spec.t;
            END;
        END IF;
    END LOOP;
END $$;

COMMIT;
