-- =============================================================================
-- Phase 4 Step 6 — drop silver.shadow_runs (R-P1-10).
--
-- The Phase 1 cutover completed; the 30-day post-cutover window has
-- elapsed; the table is archived to S3 via
-- scripts/phase4_step6_archive_shadow_runs.sh before this migration
-- runs (operator must run that script first — this SQL has no
-- archival side-effects).
--
-- After this migration:
--   - silver.shadow_runs is GONE; the diff worker has nothing to write.
--   - workspace.feature_flags rows for `ingest_pdf_hatchet_traffic_pct`
--     and `ingest_pdf_shadow_enabled` are dropped — Phase 1's traffic
--     ramp is a closed chapter.
--   - The shadow_diff + shadow_diff_scan Hatchet workflows are removed
--     from the AI worker pool in code (worker.py edit lands in the same
--     PR as this SQL).
--
-- Idempotent.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Drop the dependent flag rows.
-- ---------------------------------------------------------------------------
DELETE FROM workspace.feature_flags
 WHERE flag_name IN (
     'ingest_pdf_hatchet_traffic_pct',
     'ingest_pdf_shadow_enabled'
 );

-- ---------------------------------------------------------------------------
-- 2. Drop the table. CASCADE handles any view / function that referenced
-- it (none expected, but defensive).
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS silver.shadow_runs CASCADE;

-- ---------------------------------------------------------------------------
-- 3. Verification.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    table_exists boolean;
    flag_count int;
BEGIN
    SELECT EXISTS(
        SELECT 1 FROM information_schema.tables
         WHERE table_schema='silver' AND table_name='shadow_runs'
    ) INTO table_exists;
    SELECT count(*) INTO flag_count
      FROM workspace.feature_flags
     WHERE flag_name IN ('ingest_pdf_hatchet_traffic_pct',
                         'ingest_pdf_shadow_enabled');

    RAISE NOTICE 'Phase 4 Step 6: shadow_runs=% lingering flags=%',
                 table_exists, flag_count;
    IF table_exists OR flag_count > 0 THEN
        RAISE EXCEPTION 'shadow_runs drop incomplete';
    END IF;
END $$;
