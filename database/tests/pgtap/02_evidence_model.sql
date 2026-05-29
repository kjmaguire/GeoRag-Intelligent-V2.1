-- pgTAP tests for Module 10 Chunk 10.3 — evidence model coverage (migrations 05-06)
-- File: database/tests/pgtap/02_evidence_model.sql
--
-- Run: ./database/tests/pgtap/run.sh --filter 02
-- Requires: pgTAP extension installed in the georag database.
--
-- Closes audit finding M-A5-05: "only migrations 08–11 have pgTAP coverage".
--
-- Coverage:
--   • silver.evidence_items  (migration 2026_04_20_140000)
--   • silver.document_revisions (migration 2026_04_20_130000)
--   • silver.answer_runs     (migration 2026_04_21_100000)
--   • silver.answer_runs.partial_resolution_rate (migration 2026_04_22_110000)
--
-- Assertions (plan: 10):
--   1.  silver.evidence_items exists
--   2.  evidence_items.evidence_type column exists
--   3.  evidence_items CHECK constraint for evidence_type enum exists
--   4.  evidence_items.workspace_id column exists (FK to workspaces)
--   5.  silver.document_revisions exists
--   6.  document_revisions.document_id column exists (FK to reports)
--   7.  document_revisions UNIQUE constraint (document_id, revision_number) exists
--   8.  silver.answer_runs exists
--   9.  answer_runs.partial_resolution_rate column exists
--   10. answer_runs.workspace_id column exists (tenant FK)

BEGIN;

SELECT plan(10);

-- ── 1. silver.evidence_items exists ─────────────────────────────────────────
SELECT has_table(
    'silver', 'evidence_items',
    'silver.evidence_items table exists'
);

-- ── 2. evidence_items.evidence_type column exists ────────────────────────────
SELECT has_column(
    'silver', 'evidence_items', 'evidence_type',
    'silver.evidence_items has evidence_type column'
);

-- ── 3. CHECK constraint for evidence_type enum is present
--        (constraint name: evidence_items_type_valid per migration) ───────────
SELECT ok(
    EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE constraint_schema = 'silver'
           AND table_name        = 'evidence_items'
           AND constraint_type   = 'CHECK'
           AND constraint_name   = 'evidence_items_type_valid'
    ),
    'silver.evidence_items has evidence_items_type_valid CHECK constraint'
);

-- ── 4. evidence_items.workspace_id column exists ─────────────────────────────
SELECT has_column(
    'silver', 'evidence_items', 'workspace_id',
    'silver.evidence_items has workspace_id column (FK to workspaces)'
);

-- ── 5. silver.document_revisions exists ──────────────────────────────────────
SELECT has_table(
    'silver', 'document_revisions',
    'silver.document_revisions table exists'
);

-- ── 6. document_revisions.document_id column exists (FK to reports) ──────────
SELECT has_column(
    'silver', 'document_revisions', 'document_id',
    'silver.document_revisions has document_id column'
);

-- ── 7. UNIQUE constraint (document_id, revision_number) exists
--        Ensures each document has at most one row per revision number. ────────
SELECT ok(
    EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE constraint_schema = 'silver'
           AND table_name        = 'document_revisions'
           AND constraint_type   = 'UNIQUE'
           AND constraint_name   = 'document_revisions_unique_revision'
    ),
    'silver.document_revisions has (document_id, revision_number) UNIQUE constraint'
);

-- ── 8. silver.answer_runs exists ─────────────────────────────────────────────
SELECT has_table(
    'silver', 'answer_runs',
    'silver.answer_runs table exists'
);

-- ── 9. answer_runs.partial_resolution_rate column exists
--        (added by migration 2026_04_22_110000) ──────────────────────────────
SELECT has_column(
    'silver', 'answer_runs', 'partial_resolution_rate',
    'silver.answer_runs has partial_resolution_rate column'
);

-- ── 10. answer_runs.workspace_id column exists (tenant FK) ───────────────────
SELECT has_column(
    'silver', 'answer_runs', 'workspace_id',
    'silver.answer_runs has workspace_id column (FK to workspaces)'
);

SELECT * FROM finish();
ROLLBACK;
