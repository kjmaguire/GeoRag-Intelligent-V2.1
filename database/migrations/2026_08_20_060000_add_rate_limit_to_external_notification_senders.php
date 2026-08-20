<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Adds `usage.external_notification_senders.rate_limit_per_minute`.
 *
 * Declared only in `database/raw/phase5/10-sender-rate-limits.sql`, which is
 * applied by hand locally and by CI but never by CD — so the column is absent
 * on Azure. `src/fastapi/app/hatchet_workflows/external_notification.py:156`
 * selects it to size the per-sender Redis token bucket:
 *
 *     SELECT rate_limit_per_minute
 *       FROM usage.external_notification_senders
 *      WHERE ...
 *     -> return row["rate_limit_per_minute"] if row else None
 *
 * With the column missing the query raises rather than returning a row, so
 * per-sender rate limiting is not enforced in production at all.
 *
 * Ported verbatim from the raw file, including the sanity CHECK: non-negative
 * and capped at 10,000/minute so an operator typo cannot configure an
 * effectively unbounded sender. NULL means "no limit" and is what the env-var
 * fallback uses, so the column stays nullable; DEFAULT 60 matches the raw
 * file, which means existing senders inherit a real limit rather than silently
 * staying unlimited when the column appears.
 *
 * If you edit the raw file, edit this migration too, or delete the raw file —
 * same rule as 2026_08_19_040000_install_workflow_functions_from_raw_sql.
 *
 * Idempotent: ADD COLUMN IF NOT EXISTS, and the constraint is added only when
 * absent.
 */
return new class extends Migration
{
    private const CONSTRAINT = 'external_notification_senders_rate_limit_check';

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (! $this->tableExists('usage', 'external_notification_senders')) {
            return;
        }

        DB::statement(
            'ALTER TABLE usage.external_notification_senders
               ADD COLUMN IF NOT EXISTS rate_limit_per_minute integer NULL DEFAULT 60',
        );

        DB::statement(
            "COMMENT ON COLUMN usage.external_notification_senders.rate_limit_per_minute IS
             'Phase 5 Step 1 — per-sender token bucket capacity / minute. NULL = no limit.'",
        );

        if ($this->constraintExists(self::CONSTRAINT)) {
            return;
        }

        DB::statement(sprintf(
            'ALTER TABLE usage.external_notification_senders
               ADD CONSTRAINT %s
               CHECK (rate_limit_per_minute IS NULL OR
                      (rate_limit_per_minute >= 0 AND rate_limit_per_minute <= 10000))',
            self::CONSTRAINT,
        ));
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (! $this->tableExists('usage', 'external_notification_senders')) {
            return;
        }

        DB::statement(sprintf(
            'ALTER TABLE usage.external_notification_senders DROP CONSTRAINT IF EXISTS %s',
            self::CONSTRAINT,
        ));
        DB::statement(
            'ALTER TABLE usage.external_notification_senders
               DROP COLUMN IF EXISTS rate_limit_per_minute',
        );
    }

    private function tableExists(string $schema, string $table): bool
    {
        return DB::selectOne(
            'SELECT 1 AS present
               FROM information_schema.tables
              WHERE table_schema = ? AND table_name = ?',
            [$schema, $table],
        ) !== null;
    }

    private function constraintExists(string $name): bool
    {
        return DB::selectOne(
            'SELECT 1 AS present FROM pg_constraint WHERE conname = ?',
            [$name],
        ) !== null;
    }
};
