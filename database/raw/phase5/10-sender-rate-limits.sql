-- =============================================================================
-- Phase 5 Step 1 — per-sender rate limit (R-P4-1).
--
-- Adds `rate_limit_per_minute` to `usage.external_notification_senders`.
-- The workflow consults this column when bucketing inbound deliveries
-- in Redis. NULL means "no limit" (the env-var fallback uses NULL).
--
-- Defaults: 60/minute. Operators bump via the dashboard or directly:
--   UPDATE usage.external_notification_senders
--      SET rate_limit_per_minute = 600
--    WHERE source = 'partner-X';
--
-- Idempotent.
-- =============================================================================

ALTER TABLE usage.external_notification_senders
    ADD COLUMN IF NOT EXISTS rate_limit_per_minute integer NULL DEFAULT 60;

COMMENT ON COLUMN usage.external_notification_senders.rate_limit_per_minute IS
    'Phase 5 Step 1 — per-sender token bucket capacity / minute. NULL = no limit.';

-- Constraint: non-negative, capped at a sane 10K/min so operator typos
-- don't accidentally configure an unbounded sender.
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'external_notification_senders_rate_limit_check'
    ) THEN
        ALTER TABLE usage.external_notification_senders
            ADD CONSTRAINT external_notification_senders_rate_limit_check
            CHECK (rate_limit_per_minute IS NULL OR
                   (rate_limit_per_minute > 0 AND rate_limit_per_minute <= 10000));
    END IF;
END $$;

DO $$
DECLARE
    n int;
BEGIN
    SELECT count(*) INTO n FROM information_schema.columns
     WHERE table_schema='usage' AND table_name='external_notification_senders'
       AND column_name='rate_limit_per_minute';
    RAISE NOTICE 'Phase 5 Step 1: rate_limit_per_minute column present = %', n;
    IF n <> 1 THEN
        RAISE EXCEPTION 'Phase 5 Step 1 column add failed';
    END IF;
END $$;
