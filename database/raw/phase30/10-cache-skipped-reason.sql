-- =============================================================================
-- Phase 30 Step 1 — silver.answer_runs.cache_skipped_reason column
-- (R-P21-CACHE-SKIPPED-REASON).
--
-- The Phase 21 cache-poison fix added three skip-write paths in
-- run_deterministic_rag:
--   * "zero candidates (avoiding poison cache)"
--   * "partial_failures present (avoiding poison cache)"
--   * "downhole bypass" (Phase 29 surgical fix for gq-015 variance,
--     to be removed once Phase 30 wires DownholeLogsResult into the
--     cache pipeline properly)
--
-- All three were visible only in DEBUG/INFO log lines. Operators
-- wanting to know "why didn't this run produce a cache entry?"
-- had to grep logs. This column promotes that diagnostic into
-- structured answer_runs telemetry so dashboards and ad-hoc SQL
-- queries can break it down without log archaeology.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
-- Constraint allows the documented set plus NULL (cache write succeeded).
-- =============================================================================

ALTER TABLE silver.answer_runs
    ADD COLUMN IF NOT EXISTS cache_skipped_reason text;

-- CHECK constraint: only the documented reasons + NULL are valid.
-- ALTER ... ADD CONSTRAINT IF NOT EXISTS doesn't exist on Postgres 18
-- so guard via pg_constraint lookup.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'answer_runs_cache_skipped_reason_check'
           AND conrelid = 'silver.answer_runs'::regclass
    ) THEN
        ALTER TABLE silver.answer_runs
            ADD CONSTRAINT answer_runs_cache_skipped_reason_check
                CHECK (cache_skipped_reason IS NULL OR cache_skipped_reason IN (
                    'zero_candidates',
                    'partial_failures',
                    'downhole_bypass_legacy',
                    'schema_validation_failed'
                ));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_answer_runs_cache_skipped_reason
    ON silver.answer_runs (cache_skipped_reason)
    WHERE cache_skipped_reason IS NOT NULL;

COMMENT ON COLUMN silver.answer_runs.cache_skipped_reason IS
    'Why this run did not write a retrieval cache entry. NULL = cache write succeeded. Phase 30 R-P21-CACHE-SKIPPED-REASON.';

-- Sanity: column exists + constraint enforces enum
DO $$
DECLARE
    col_exists boolean;
    chk_exists boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'silver'
           AND table_name = 'answer_runs'
           AND column_name = 'cache_skipped_reason'
    ) INTO col_exists;

    SELECT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'answer_runs_cache_skipped_reason_check'
           AND conrelid = 'silver.answer_runs'::regclass
    ) INTO chk_exists;

    RAISE NOTICE 'Phase 30 cache_skipped_reason: column_exists=% check_exists=%',
        col_exists, chk_exists;

    IF NOT col_exists THEN
        RAISE EXCEPTION 'cache_skipped_reason column missing after migration';
    END IF;
    IF NOT chk_exists THEN
        RAISE EXCEPTION 'cache_skipped_reason CHECK constraint missing after migration';
    END IF;
END $$;
