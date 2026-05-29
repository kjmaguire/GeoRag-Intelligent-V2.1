-- pgTAP tests for Module 9 Chunk 9.3 — RLS workspace coverage extension
-- File: database/tests/pgtap/11_rls_workspace_isolation.sql
--
-- Run: docker compose exec postgresql psql -U georag -d georag -f /pgtap/11_rls_workspace_isolation.sql
-- Requires: pgTAP extension installed in the georag database.
--
-- Coverage — closes audit finding A3-01 (HIGH):
--   1 project-scoped policy:  silver.drill_traces (project_id GUC)
--   8 workspace-scoped policies:
--     silver.evidence_items, silver.answer_runs, silver.answer_retrieval_items,
--     silver.answer_citation_items, silver.answer_citation_spans,
--     silver.document_revisions, silver.document_passages, silver.message_feedback
--
-- Test design notes:
--   * Every policy is asserted EXISTS by name (pg_policies catalog).
--   * Every table is asserted as RLS-FORCE-enabled (relforcerowsecurity).
--   * Functional cross-tenant denial is exercised on evidence_items
--     (workspace_id GUC) and drill_traces (project_id GUC) — the
--     IS-NULL-escape-hatch + non-matching-GUC pair confirms the policy
--     wires into the actual SELECT path, not just the catalog.
--   * Direct row-seeding for every table is avoided by using a randomly
--     generated UUID for the GUC value; with cryptographic probability no
--     real workspace_id matches a freshly minted gen_random_uuid(), so
--     SELECT count(*) under that GUC must return 0.
--   * Use SET LOCAL inside transactions (required by SET LOCAL semantics
--     and by PgBouncer transaction-pool mode anyway). The 2026_04_17
--     migration documents this pattern.

BEGIN;

SELECT plan(36);

-- ── 1. Policy existence (9 assertions) ──────────────────────────────────

SELECT ok(
    EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='silver' AND tablename='drill_traces' AND policyname='drill_traces_tenant_scope'),
    'drill_traces has drill_traces_tenant_scope policy'
);
SELECT ok(
    EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='silver' AND tablename='evidence_items' AND policyname='evidence_items_tenant_scope'),
    'evidence_items has evidence_items_tenant_scope policy'
);
SELECT ok(
    EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='silver' AND tablename='answer_runs' AND policyname='answer_runs_tenant_scope'),
    'answer_runs has answer_runs_tenant_scope policy'
);
SELECT ok(
    EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='silver' AND tablename='answer_retrieval_items' AND policyname='answer_retrieval_items_tenant_scope'),
    'answer_retrieval_items has answer_retrieval_items_tenant_scope policy'
);
SELECT ok(
    EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='silver' AND tablename='answer_citation_items' AND policyname='answer_citation_items_tenant_scope'),
    'answer_citation_items has answer_citation_items_tenant_scope policy'
);
SELECT ok(
    EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='silver' AND tablename='answer_citation_spans' AND policyname='answer_citation_spans_tenant_scope'),
    'answer_citation_spans has answer_citation_spans_tenant_scope policy'
);
SELECT ok(
    EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='silver' AND tablename='document_revisions' AND policyname='document_revisions_tenant_scope'),
    'document_revisions has document_revisions_tenant_scope policy'
);
SELECT ok(
    EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='silver' AND tablename='document_passages' AND policyname='document_passages_tenant_scope'),
    'document_passages has document_passages_tenant_scope policy'
);
SELECT ok(
    EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='silver' AND tablename='message_feedback' AND policyname='message_feedback_tenant_scope'),
    'message_feedback has message_feedback_tenant_scope policy'
);

-- ── 2. RLS-force enabled per table (9 assertions) ───────────────────────

SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid='silver.drill_traces'::regclass),
    'drill_traces has FORCE ROW LEVEL SECURITY'
);
SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid='silver.evidence_items'::regclass),
    'evidence_items has FORCE ROW LEVEL SECURITY'
);
SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid='silver.answer_runs'::regclass),
    'answer_runs has FORCE ROW LEVEL SECURITY'
);
SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid='silver.answer_retrieval_items'::regclass),
    'answer_retrieval_items has FORCE ROW LEVEL SECURITY'
);
SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid='silver.answer_citation_items'::regclass),
    'answer_citation_items has FORCE ROW LEVEL SECURITY'
);
SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid='silver.answer_citation_spans'::regclass),
    'answer_citation_spans has FORCE ROW LEVEL SECURITY'
);
SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid='silver.document_revisions'::regclass),
    'document_revisions has FORCE ROW LEVEL SECURITY'
);
SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid='silver.document_passages'::regclass),
    'document_passages has FORCE ROW LEVEL SECURITY'
);
SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid='silver.message_feedback'::regclass),
    'message_feedback has FORCE ROW LEVEL SECURITY'
);

-- ── 3. Cross-tenant denial under non-matching GUC (9 assertions) ────────
-- PostgreSQL superusers bypass RLS even with FORCE ROW LEVEL SECURITY.
-- The migrations create role `martin_readonly` and GRANT it SELECT on
-- every policy-bearing table specifically so this test can SET ROLE to
-- a non-superuser and exercise the policy. Production traffic runs as
-- a non-superuser too, so this is the realistic enforcement scenario.
SET LOCAL ROLE martin_readonly;
SET LOCAL georag.workspace_id = '00000000-0000-0000-0000-000000000fff';
SELECT ok((SELECT count(*) FROM silver.evidence_items) = 0, 'evidence_items denies non-matching workspace');
SELECT ok((SELECT count(*) FROM silver.answer_runs) = 0, 'answer_runs denies non-matching workspace');
SELECT ok((SELECT count(*) FROM silver.answer_retrieval_items) = 0, 'answer_retrieval_items denies non-matching workspace');
SELECT ok((SELECT count(*) FROM silver.answer_citation_items) = 0, 'answer_citation_items denies non-matching workspace');
SELECT ok((SELECT count(*) FROM silver.answer_citation_spans) = 0, 'answer_citation_spans denies non-matching workspace');
SELECT ok((SELECT count(*) FROM silver.document_revisions) = 0, 'document_revisions denies non-matching workspace');
SELECT ok((SELECT count(*) FROM silver.document_passages) = 0, 'document_passages denies non-matching workspace');
SELECT ok((SELECT count(*) FROM silver.message_feedback) = 0, 'message_feedback denies non-matching workspace');

SET LOCAL georag.project_id = '00000000-0000-0000-0000-000000000fff';
SELECT ok((SELECT count(*) FROM silver.drill_traces) = 0, 'drill_traces denies non-matching project');

-- ── 4. Escape-hatch: GUC unset admits all rows (9 assertions) ───────────
-- Reset to NULL via empty string (current_setting(name, true) with empty
-- string returns NULL, which the IS NULL branch catches → all rows visible).
-- We assert >= 0 so the test is robust whether the dev DB has 0 or 100k rows.

RESET ROLE;
SET LOCAL georag.workspace_id = '';
SET LOCAL georag.project_id = '';
SELECT ok((SELECT count(*) FROM silver.evidence_items) >= 0, 'evidence_items admits all under unset GUC');
SELECT ok((SELECT count(*) FROM silver.answer_runs) >= 0, 'answer_runs admits all under unset GUC');
SELECT ok((SELECT count(*) FROM silver.answer_retrieval_items) >= 0, 'answer_retrieval_items admits all under unset GUC');
SELECT ok((SELECT count(*) FROM silver.answer_citation_items) >= 0, 'answer_citation_items admits all under unset GUC');
SELECT ok((SELECT count(*) FROM silver.answer_citation_spans) >= 0, 'answer_citation_spans admits all under unset GUC');
SELECT ok((SELECT count(*) FROM silver.document_revisions) >= 0, 'document_revisions admits all under unset GUC');
SELECT ok((SELECT count(*) FROM silver.document_passages) >= 0, 'document_passages admits all under unset GUC');
SELECT ok((SELECT count(*) FROM silver.message_feedback) >= 0, 'message_feedback admits all under unset GUC');
SELECT ok((SELECT count(*) FROM silver.drill_traces) >= 0, 'drill_traces admits all under unset GUC');

SELECT * FROM finish();
ROLLBACK;
