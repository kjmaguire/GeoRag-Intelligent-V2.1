<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Sibling to 2026_05_14_140000 / 140100 / 140200 — provision
 * silver.support_packets in the Laravel test DB.
 *
 * Production gets this table from raw SQL (database/raw/phase0/120-phase0-
 * step6-support-packets.sql). It's later ALTERed by Laravel migration
 * 2026_05_19_180200_add_trace_id_to_silver_support_packets, which fails on
 * a fresh test DB without this mirror.
 *
 * Column shape mirrors raw/phase0/120 exactly. RLS is enabled here too
 * (raw SQL enables + forces RLS with a tenant_isolation policy on
 * `app.workspace_id`); leaving RLS on in test keeps behaviour consistent
 * with prod when tests do exercise this table.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.support_packets (
                id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id        UUID         NOT NULL REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                incident_id         TEXT         NOT NULL,
                storage_uri         TEXT         NOT NULL,
                storage_tier        TEXT         NOT NULL DEFAULT 'warm'
                    CHECK (storage_tier IN ('hot','warm','cold')),
                bundle_bytes        BIGINT       NOT NULL DEFAULT 0,
                contents_summary    JSONB        NOT NULL DEFAULT '{}'::jsonb,
                assembled_by        TEXT         NOT NULL DEFAULT 'Support Packet Agent',
                assembled_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                requested_by        BIGINT       NULL,
                expires_at          TIMESTAMPTZ  NULL,
                status              TEXT         NOT NULL DEFAULT 'available'
                    CHECK (status IN ('available','expired','purged'))
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS support_packets_workspace_idx
                       ON silver.support_packets (workspace_id, assembled_at DESC)');
        DB::statement('CREATE INDEX IF NOT EXISTS support_packets_incident_idx
                       ON silver.support_packets (incident_id, assembled_at DESC)');

        // RLS — mirrors raw/phase0/120 (ENABLE + FORCE + tenant_isolation).
        // Skip silently if the policy already exists (idempotent re-runs).
        DB::statement('ALTER TABLE silver.support_packets ENABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE silver.support_packets FORCE ROW LEVEL SECURITY');
        DB::statement('DROP POLICY IF EXISTS tenant_isolation ON silver.support_packets');
        DB::statement(<<<'SQL'
            CREATE POLICY tenant_isolation ON silver.support_packets
              USING (workspace_id::text = current_setting('app.workspace_id', true))
              WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true))
        SQL);

        DB::statement('GRANT SELECT, INSERT, UPDATE ON silver.support_packets TO georag_app');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS silver.support_packets CASCADE');
    }
};
