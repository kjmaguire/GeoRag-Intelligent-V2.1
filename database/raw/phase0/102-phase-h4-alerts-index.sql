-- =============================================================================
-- Phase H4 — partial index for the alerts inbox (§7 cockpit + audit-anchored
-- *.alert convention).
--
-- The /api/v1/admin/alerts-inbox endpoint runs:
--     WHERE action_type LIKE '%.alert'
--     ORDER BY created_at DESC
-- ...with a LATERAL join on action_type || '.acknowledged' + target_id to
-- detect acknowledged rows. The existing (action_type, created_at DESC) index
-- on audit_ledger does not help the LIKE-suffix scan; a partial index on rows
-- where action_type LIKE '%.alert' cuts the I/O profile dramatically while
-- keeping the index small (alerts are a tiny fraction of total audit rows).
--
-- Two indexes:
--   1. (created_at DESC) WHERE action_type LIKE '%.alert'
--        - serves the inbox listing ORDER BY
--   2. (action_type, target_id) WHERE action_type LIKE '%.acknowledged'
--        - serves the LATERAL ack lookup
--
-- Both are CONCURRENTLY-safe; we omit CONCURRENTLY here because this file
-- is idempotent BEGIN…COMMIT and intended for fresh installs. Production
-- deploys with an existing audit_ledger should run the CREATE INDEX
-- statements manually with CONCURRENTLY.
-- =============================================================================

BEGIN;

CREATE INDEX IF NOT EXISTS audit_ledger_alerts_idx
    ON audit.audit_ledger (created_at DESC)
    WHERE action_type LIKE '%.alert';

CREATE INDEX IF NOT EXISTS audit_ledger_acks_idx
    ON audit.audit_ledger (action_type, target_id)
    WHERE action_type LIKE '%.acknowledged';

COMMENT ON INDEX audit.audit_ledger_alerts_idx IS
    'Phase H4 — partial index for /admin/alerts-inbox listing. Filters to '
    '*.alert rows; ordered by created_at DESC for the inbox sort.';

COMMENT ON INDEX audit.audit_ledger_acks_idx IS
    'Phase H4 — partial index supporting the LATERAL ack lookup in '
    '/admin/alerts-inbox (action_type = <alert>.acknowledged + target_id).';

COMMIT;
