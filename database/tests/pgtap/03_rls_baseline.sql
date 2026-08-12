-- pgTAP tests for Module 10 Chunk 10.3 — RLS baseline coverage (migrations 01-07 scope)
-- File: database/tests/pgtap/03_rls_baseline.sql
--
-- Run: ./database/tests/pgtap/run.sh --filter 03
-- Requires: pgTAP extension installed in the georag database.
--
-- Closes audit finding M-A5-05: "only migrations 08–11 have pgTAP coverage".
--
-- This file covers the 11 silver tables that carry GUC-aware RLS policies,
-- introduced across two migrations:
--
--   • 2026_04_17_120200_replace_toothless_rls_with_guc_aware_policies
--       Tables: silver.collars, silver.samples
--       Policy family: *_workspace_isolation (v2 renames included)
--
--   • 2026_04_22_170000_extend_rls_workspace_coverage
--       Tables: silver.drill_traces, silver.evidence_items, silver.answer_runs,
--               silver.answer_retrieval_items, silver.answer_citation_items,
--               silver.answer_citation_spans, silver.document_revisions,
--               silver.document_passages, silver.message_feedback
--       Policy names: originally <table>_tenant_scope; RENAMED by the
--       2026-05/06 tenancy normalization:
--         - the 8 workspace tables now carry <table>_workspace_isolation
--           (2026_05_25_180924_replace_broken_guc_rls_policies_with_canonical,
--            body normalized by 2026_05_25_184857)
--         - drill_traces now carries tenant_isolation
--           (2026_05_30_010000_enable_rls_silver_drill_traces; the legacy
--            drill_traces_workspace_isolation is dropped by 2026_05_30_020000)
--
-- NOTE: 11_rls_workspace_isolation.sql (Module 9) covers the 9 workspace-scoped
-- tables with functional denial tests. This file covers the 2 project-scoped
-- tables (collars + samples) that were NOT in that file, plus validates that all
-- 11 tables have FORCE ROW LEVEL SECURITY enabled — asserting the combined
-- baseline that migrations 120200 + 220000 produce.
--
-- Assertions (plan: 15):
--   1.  collars has RLS enabled
--   2.  collars has FORCE ROW LEVEL SECURITY
--   3.  collars has a workspace_isolation policy
--   4.  samples has RLS enabled
--   5.  samples has FORCE ROW LEVEL SECURITY
--   6.  samples has a workspace_isolation policy
--   7.  drill_traces has relrowsecurity = true
--   8.  drill_traces has relforcerowsecurity = true
--   9.  drill_traces has a tenant_isolation policy
--   10. evidence_items has relforcerowsecurity = true
--   11. evidence_items has a workspace_isolation policy
--   12. answer_runs has relforcerowsecurity = true
--   13. answer_runs has a workspace_isolation policy
--   14. document_revisions has relforcerowsecurity = true
--   15. document_revisions has a workspace_isolation policy
--
-- The remaining workspace-scoped tables (answer_retrieval_items, answer_citation_items,
-- answer_citation_spans, document_passages, message_feedback) are already asserted
-- in 11_rls_workspace_isolation.sql with functional denial tests. This file adds
-- the missing collars/samples coverage and verifies the full 11-table baseline.

BEGIN;

SELECT plan(15);

-- ── 1–3. silver.collars ──────────────────────────────────────────────────────

SELECT ok(
    (SELECT relrowsecurity FROM pg_class WHERE oid = 'silver.collars'::regclass),
    'silver.collars has ROW LEVEL SECURITY enabled'
);

SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid = 'silver.collars'::regclass),
    'silver.collars has FORCE ROW LEVEL SECURITY'
);

SELECT ok(
    EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'silver'
           AND tablename  = 'collars'
           -- Renamed in the 2026-05/06 tenancy normalization; assert the
           -- workspace_isolation family so a future _v3 doesn't re-break this.
           AND policyname LIKE '%workspace_isolation%'
    ),
    'silver.collars has a workspace_isolation policy'
);

-- ── 4–6. silver.samples ──────────────────────────────────────────────────────

SELECT ok(
    (SELECT relrowsecurity FROM pg_class WHERE oid = 'silver.samples'::regclass),
    'silver.samples has ROW LEVEL SECURITY enabled'
);

SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid = 'silver.samples'::regclass),
    'silver.samples has FORCE ROW LEVEL SECURITY'
);

SELECT ok(
    EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'silver'
           AND tablename  = 'samples'
           AND policyname LIKE '%workspace_isolation%'
    ),
    'silver.samples has a workspace_isolation policy'
);

-- ── 7–9. silver.drill_traces ─────────────────────────────────────────────────

SELECT ok(
    (SELECT relrowsecurity FROM pg_class WHERE oid = 'silver.drill_traces'::regclass),
    'silver.drill_traces has ROW LEVEL SECURITY enabled'
);

SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid = 'silver.drill_traces'::regclass),
    'silver.drill_traces has FORCE ROW LEVEL SECURITY'
);

SELECT ok(
    EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'silver'
           AND tablename  = 'drill_traces'
           -- Renamed in the 2026-05/06 tenancy normalization: the old
           -- drill_traces_tenant_scope was dropped and the canonical policy
           -- is now named tenant_isolation (2026_05_30_010000; the legacy
           -- drill_traces_workspace_isolation was dropped by 2026_05_30_020000).
           AND policyname LIKE '%tenant_isolation%'
    ),
    'silver.drill_traces has a tenant_isolation policy'
);

-- ── 10–11. silver.evidence_items (spot-check workspace-scoped group) ─────────

SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid = 'silver.evidence_items'::regclass),
    'silver.evidence_items has FORCE ROW LEVEL SECURITY'
);

SELECT ok(
    EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'silver'
           AND tablename  = 'evidence_items'
           -- Renamed in the 2026-05/06 tenancy normalization: the broken
           -- evidence_items_tenant_scope (legacy georag.* GUC) was replaced
           -- by the canonical workspace_isolation family (2026_05_25_180924).
           AND policyname LIKE '%workspace_isolation%'
    ),
    'silver.evidence_items has a workspace_isolation policy'
);

-- ── 12–13. silver.answer_runs ────────────────────────────────────────────────

SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid = 'silver.answer_runs'::regclass),
    'silver.answer_runs has FORCE ROW LEVEL SECURITY'
);

SELECT ok(
    EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'silver'
           AND tablename  = 'answer_runs'
           -- Renamed in the 2026-05/06 tenancy normalization (2026_05_25_180924).
           AND policyname LIKE '%workspace_isolation%'
    ),
    'silver.answer_runs has a workspace_isolation policy'
);

-- ── 14–15. silver.document_revisions ─────────────────────────────────────────

SELECT ok(
    (SELECT relforcerowsecurity FROM pg_class WHERE oid = 'silver.document_revisions'::regclass),
    'silver.document_revisions has FORCE ROW LEVEL SECURITY'
);

SELECT ok(
    EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'silver'
           AND tablename  = 'document_revisions'
           -- Renamed in the 2026-05/06 tenancy normalization (2026_05_25_180924).
           AND policyname LIKE '%workspace_isolation%'
    ),
    'silver.document_revisions has a workspace_isolation policy'
);

SELECT * FROM finish();
ROLLBACK;
