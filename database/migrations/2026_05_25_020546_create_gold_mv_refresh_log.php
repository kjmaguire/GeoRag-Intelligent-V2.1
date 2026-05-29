<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Phase 1 of the ingestion reliability spec — create gold.mv_refresh_log.
 *
 * One row per materialised view refresh attempt. Consumed by Phase 2's
 * `workspace.data_updated` emitter (which only fires after a successful
 * refresh entry newer than the run's completed_at), and by the nightly Tier 3
 * audit's staleness check.
 *
 * Logged whether the refresh succeeded, failed, or was skipped. status='failed'
 * rows are an alert signal — we don't want to silently swallow refresh errors
 * the way the previous architecture did.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('CREATE SCHEMA IF NOT EXISTS gold');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS gold.mv_refresh_log (
                id            bigserial   PRIMARY KEY,
                view_name     text        NOT NULL,
                workspace_id  uuid,
                started_at    timestamptz NOT NULL DEFAULT now(),
                finished_at   timestamptz,
                duration_ms   integer,
                rows_before   bigint,
                rows_after    bigint,
                triggered_by  text,
                status        text        NOT NULL DEFAULT 'started',
                error         jsonb,
                CONSTRAINT mv_refresh_log_status_valid
                    CHECK (status IN ('started','completed','failed','skipped')),
                CONSTRAINT mv_refresh_log_triggered_by_valid
                    CHECK (triggered_by IS NULL OR triggered_by IN (
                        'ingestion','nightly_integrity','manual'
                    ))
            )
        SQL);

        // workspace.data_updated emitter looks up "most recent successful
        // refresh for this view + workspace" — index that hot path.
        DB::statement(
            "CREATE INDEX IF NOT EXISTS mv_refresh_log_view_workspace_finished_idx
             ON gold.mv_refresh_log (view_name, workspace_id, finished_at DESC)
             WHERE status = 'completed'",
        );

        // Alerting query — failures in the last 24h.
        DB::statement(
            "CREATE INDEX IF NOT EXISTS mv_refresh_log_failures_idx
             ON gold.mv_refresh_log (started_at DESC)
             WHERE status = 'failed'",
        );
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS gold.mv_refresh_log');
    }
};
