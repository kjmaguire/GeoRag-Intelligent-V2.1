-- pgTAP tests for Phase 3 (2026-05-22) — OCR confidence + method columns
-- on silver.document_passages.
--
-- Migration: 2026_05_22_020000_add_ocr_confidence_to_document_passages
--
-- Run: ./database/tests/pgtap/run.sh --filter 12
-- Requires: pgTAP extension installed in the georag database.
--
-- Coverage:
--   • ocr_confidence column exists with type numeric(5,4)
--   • ocr_method column exists with type varchar(50)
--   • Both columns default to NULL
--   • Range CHECK constraint exists on ocr_confidence (0..1)
--   • Enum CHECK constraint exists on ocr_method
--   • Partial index for low-confidence rows exists
--   • All valid ocr_method values accepted
--   • Invalid ocr_method rejected
--   • ocr_confidence < 0 rejected
--   • ocr_confidence > 1 rejected

BEGIN;

SELECT plan(13);

-- ── 1. ocr_confidence column exists ─────────────────────────────────────────
SELECT has_column(
    'silver', 'document_passages', 'ocr_confidence',
    'silver.document_passages has ocr_confidence column'
);

-- ── 2. ocr_confidence type is numeric ────────────────────────────────────────
SELECT col_type_is(
    'silver', 'document_passages', 'ocr_confidence', 'numeric(5,4)',
    'ocr_confidence is numeric(5,4)'
);

-- ── 3. ocr_confidence is nullable ────────────────────────────────────────────
SELECT col_is_null(
    'silver', 'document_passages', 'ocr_confidence',
    'ocr_confidence is nullable (NULL = text-layer extraction)'
);

-- ── 4. ocr_method column exists ──────────────────────────────────────────────
SELECT has_column(
    'silver', 'document_passages', 'ocr_method',
    'silver.document_passages has ocr_method column'
);

-- ── 5. ocr_method type is varchar(50) ────────────────────────────────────────
SELECT col_type_is(
    'silver', 'document_passages', 'ocr_method', 'character varying(50)',
    'ocr_method is varchar(50)'
);

-- ── 6. ocr_method is nullable ────────────────────────────────────────────────
SELECT col_is_null(
    'silver', 'document_passages', 'ocr_method',
    'ocr_method is nullable (NULL = pre-Phase-3 extraction)'
);

-- ── 7. Range constraint exists ───────────────────────────────────────────────
-- pgTAP has no 4-arg has_check(schema, table, constraint_name, desc);
-- col_has_check asserts the column carries a CHECK constraint, and tests
-- 10-13 below exercise the range/enum semantics behaviourally.
SELECT col_has_check(
    'silver', 'document_passages', 'ocr_confidence',
    'ocr_confidence has a CHECK constraint (document_passages_ocr_confidence_range)'
);

-- ── 8. Enum constraint exists ────────────────────────────────────────────────
SELECT col_has_check(
    'silver', 'document_passages', 'ocr_method',
    'ocr_method has a CHECK constraint (document_passages_ocr_method_check)'
);

-- ── 9. Partial index exists ──────────────────────────────────────────────────
SELECT has_index(
    'silver', 'document_passages', 'idx_document_passages_low_ocr_confidence',
    'idx_document_passages_low_ocr_confidence partial index exists'
);

-- ── 10. ocr_confidence rejects negative values ──────────────────────────────
SELECT throws_ok(
    $$
    INSERT INTO silver.document_passages
      (passage_id, document_id, workspace_id, revision_number,
       text, text_hash, ordinal, ocr_confidence)
    VALUES (
      gen_random_uuid(),
      (SELECT report_id FROM silver.reports LIMIT 1),
      (SELECT workspace_id FROM silver.workspaces LIMIT 1),
      1, 'pgtap test invalid neg', repeat('a', 64), 0, -0.1
    )
    $$,
    NULL,
    'ocr_confidence = -0.1 rejected by range constraint'
);

-- ── 11. ocr_confidence rejects > 1 ──────────────────────────────────────────
SELECT throws_ok(
    $$
    INSERT INTO silver.document_passages
      (passage_id, document_id, workspace_id, revision_number,
       text, text_hash, ordinal, ocr_confidence)
    VALUES (
      gen_random_uuid(),
      (SELECT report_id FROM silver.reports LIMIT 1),
      (SELECT workspace_id FROM silver.workspaces LIMIT 1),
      1, 'pgtap test invalid >1', repeat('b', 64), 0, 1.5
    )
    $$,
    NULL,
    'ocr_confidence = 1.5 rejected by range constraint'
);

-- ── 12. ocr_method rejects unknown engine name ──────────────────────────────
SELECT throws_ok(
    $$
    INSERT INTO silver.document_passages
      (passage_id, document_id, workspace_id, revision_number,
       text, text_hash, ordinal, ocr_method)
    VALUES (
      gen_random_uuid(),
      (SELECT report_id FROM silver.reports LIMIT 1),
      (SELECT workspace_id FROM silver.workspaces LIMIT 1),
      1, 'pgtap test invalid engine', repeat('c', 64), 0, 'easyocr'
    )
    $$,
    NULL,
    'ocr_method = ''easyocr'' rejected by enum CHECK'
);

-- ── 13. Every allowed ocr_method value is accepted ──────────────────────────
-- We test each one as a separate sanity insert that should succeed. Wrap in
-- a savepoint so the inserts roll back individually and don't leak rows
-- into other tests.
--
-- 'docling_rapidocr' is deliberately absent. Docling left the image on
-- 2026-07-29 and migration 2026_08_20_070000 dropped the label from the
-- CHECK: the constraint is the only machine-readable statement of which
-- OCR engines this pipeline actually has, so a stale entry lets a typo'd
-- or resurrected label pass validation instead of failing loudly. This
-- list tracks that migration's CURRENT const, not history.
DO $$
DECLARE
    methods text[] := ARRAY[
        'fitz_native',
        'pdfplumber_native',
        'tesseract',
        'document_intelligence',
        'unavailable'
    ];
    m text;
    report_id_val uuid := (SELECT report_id FROM silver.reports LIMIT 1);
    workspace_id_val uuid := (SELECT workspace_id FROM silver.workspaces LIMIT 1);
BEGIN
    FOREACH m IN ARRAY methods LOOP
        BEGIN
            INSERT INTO silver.document_passages
              (passage_id, document_id, workspace_id, revision_number,
               text, text_hash, ordinal, ocr_method, ocr_confidence)
            VALUES (
              gen_random_uuid(), report_id_val, workspace_id_val,
              1, 'pgtap test method ' || m, md5(random()::text) || md5(m), 0,
              m, 0.5
            );
            RAISE NOTICE 'ocr_method=% accepted', m;
        EXCEPTION WHEN check_violation THEN
            RAISE EXCEPTION 'Valid ocr_method % was rejected', m;
        END;
    END LOOP;
END $$;

SELECT pass('All five valid ocr_method values accepted (fitz_native, pdfplumber_native, tesseract, document_intelligence, unavailable)');

SELECT * FROM finish();

-- Roll back all the test rows so the harness doesn't pollute production.
ROLLBACK;
