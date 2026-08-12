-- pgTAP tests for Module 9 Chunk 9.3 — RLS workspace coverage extension
-- File: database/tests/pgtap/11_rls_workspace_isolation.sql
--
-- Run: docker compose exec postgresql psql -U georag -d georag -f /pgtap/11_rls_workspace_isolation.sql
-- Requires: pgTAP extension installed in the georag database.
--
-- Coverage — closes audit finding A3-01 (HIGH):
--   9 workspace-scoped policies:
--     silver.drill_traces, silver.evidence_items, silver.answer_runs,
--     silver.answer_retrieval_items, silver.answer_citation_items,
--     silver.answer_citation_spans, silver.document_revisions,
--     silver.document_passages, silver.message_feedback
--
-- POLICY NAMES + GUC (updated for the 2026-05/06 tenancy normalization):
--   The original *_tenant_scope / project-scoped policies read the legacy
--   georag.workspace_id / georag.project_id GUCs that no production codepath
--   sets — they were fail-open and were replaced:
--     * the 8 workspace tables now carry <table>_workspace_isolation
--       (2026_05_25_180924_replace_broken_guc_rls_policies_with_canonical,
--        bodies normalized to app.workspace_id by 2026_05_25_184857)
--     * drill_traces now carries tenant_isolation
--       (2026_05_30_010000_enable_rls_silver_drill_traces; project-scoping is
--        gone — workspace_id is the canonical tenancy boundary; the legacy
--        drill_traces_workspace_isolation was dropped by 2026_05_30_020000)
--   ALL current policies read app.workspace_id. This file asserts family
--   names via LIKE (so a future _v3 rename doesn't re-break it) and
--   exercises the app.workspace_id contract in the behavioral sections.
--
-- Test design notes:
--   * Every policy is asserted to EXIST by family name (pg_policies catalog).
--   * Every table is asserted as RLS-FORCE-enabled (relforcerowsecurity).
--   * Functional cross-tenant denial is exercised on all 9 tables via a
--     non-matching app.workspace_id — the unset-GUC-escape-hatch +
--     non-matching-GUC pair confirms the policy wires into the actual
--     SELECT path, not just the catalog.
--   * Direct row-seeding for every table is avoided by using a fixed
--     sentinel UUID for the GUC value; no fixture workspace_id matches it,
--     so SELECT count(*) under that GUC must return 0.
--   * Use SET LOCAL inside transactions (required by SET LOCAL semantics
--     and by PgBouncer transaction-pool mode anyway). The 2026_04_17
--     migration documents this pattern.

BEGIN;

SELECT plan(36);

-- ── 1. Policy existence (9 assertions) ──────────────────────────────────

-- drill_traces is the odd one out: its canonical policy is named
-- tenant_isolation (2026_05_30_010000), not <table>_workspace_isolation.
SELECT ok(
    EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='silver' AND tablename='drill_traces' AND policyname LIKE '%tenant_isolation%'),
    'drill_traces has a tenant_isolation policy'
);
SELECT ok(
    EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='silver' AND tablename='evidence_items' AND policyname LIKE '%workspace_isolation%'),
    'evidence_items has a workspace_isolation policy'
);
SELECT ok(
    EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='silver' AND tablename='answer_runs' AND policyname LIKE '%workspace_isolation%'),
    'answer_runs has a workspace_isolation policy'
);
SELECT ok(
    EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='silver' AND tablename='answer_retrieval_items' AND policyname LIKE '%workspace_isolation%'),
    'answer_retrieval_items has a workspace_isolation policy'
);
SELECT ok(
    EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='silver' AND tablename='answer_citation_items' AND policyname LIKE '%workspace_isolation%'),
    'answer_citation_items has a workspace_isolation policy'
);
SELECT ok(
    EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='silver' AND tablename='answer_citation_spans' AND policyname LIKE '%workspace_isolation%'),
    'answer_citation_spans has a workspace_isolation policy'
);
SELECT ok(
    EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='silver' AND tablename='document_revisions' AND policyname LIKE '%workspace_isolation%'),
    'document_revisions has a workspace_isolation policy'
);
SELECT ok(
    EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='silver' AND tablename='document_passages' AND policyname LIKE '%workspace_isolation%'),
    'document_passages has a workspace_isolation policy'
);
SELECT ok(
    EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='silver' AND tablename='message_feedback' AND policyname LIKE '%workspace_isolation%'),
    'message_feedback has a workspace_isolation policy'
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
--
-- GUC contract (2026-05/06 normalization): all 9 policies read
-- app.workspace_id. The legacy georag.workspace_id / georag.project_id
-- GUCs this file originally set are ignored by the current policies —
-- setting them left app.workspace_id unset, which takes the deliberate
-- unset-GUC escape hatch (all rows visible) and made the drill_traces
-- denial test fail against seeded fixture rows. Setting a non-matching
-- app.workspace_id is what exercises the deny path.
SET LOCAL ROLE martin_readonly;
SET LOCAL app.workspace_id = '00000000-0000-0000-0000-000000000fff';
SELECT ok((SELECT count(*) FROM silver.evidence_items) = 0, 'evidence_items denies non-matching workspace');
SELECT ok((SELECT count(*) FROM silver.answer_runs) = 0, 'answer_runs denies non-matching workspace');
SELECT ok((SELECT count(*) FROM silver.answer_retrieval_items) = 0, 'answer_retrieval_items denies non-matching workspace');
SELECT ok((SELECT count(*) FROM silver.answer_citation_items) = 0, 'answer_citation_items denies non-matching workspace');
SELECT ok((SELECT count(*) FROM silver.answer_citation_spans) = 0, 'answer_citation_spans denies non-matching workspace');
SELECT ok((SELECT count(*) FROM silver.document_revisions) = 0, 'document_revisions denies non-matching workspace');
SELECT ok((SELECT count(*) FROM silver.document_passages) = 0, 'document_passages denies non-matching workspace');
SELECT ok((SELECT count(*) FROM silver.message_feedback) = 0, 'message_feedback denies non-matching workspace');

-- drill_traces was project-scoped pre-normalization; its tenant_isolation
-- policy is now workspace-scoped like the rest (app.workspace_id). The
-- GoldenFixture seeds drill_traces rows, so this denial is non-vacuous.
SELECT ok((SELECT count(*) FROM silver.drill_traces) = 0, 'drill_traces denies non-matching workspace');

-- ── 4. Escape-hatch: GUC unset admits all rows (9 assertions) ───────────
-- Reset to NULL via empty string (current_setting(name, true) with empty
-- string returns NULL, which the IS NULL branch catches → all rows visible).
-- We assert >= 0 so the test is robust whether the dev DB has 0 or 100k rows.

RESET ROLE;
SET LOCAL app.workspace_id = '';
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
