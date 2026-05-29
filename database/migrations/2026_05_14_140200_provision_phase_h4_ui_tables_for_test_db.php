<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Sibling to 2026_05_14_140000 / 140100 — provision the Phase H4 UI tables
 * that production gets from raw SQL (database/raw/phase0/101-phase-h4-ui-
 * tables.sql) but the Laravel test DB does not.
 *
 * Only the tables actually referenced by downstream Laravel migrations are
 * mirrored here:
 *   - silver.qp_credentials — ALTERed by 2026_05_19_180100 to ENABLE RLS.
 *
 * Skipped intentionally:
 *   - silver.workspace_settings — no Laravel migration references it.
 *   - workflow.activepieces_channels — only dropped (2026_05_17_120000)
 *     with IF EXISTS, so a missing table is a no-op.
 *
 * `CREATE TABLE IF NOT EXISTS` is a no-op on production where the raw
 * SQL ran first; on the test DB the table is created fresh with the same
 * column shape.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // ─────────────────────── silver.qp_credentials ──────────────────
        // Cross-workspace QP registry (§29.6). Not RLS-scoped at creation
        // (access is gated at the Laravel 'admin' Gate in prod) but
        // 2026_05_19_180100 turns user-scoped RLS on with a fail-open
        // policy. Column shape mirrors raw/phase0/101 exactly.
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.qp_credentials (
                qp_credential_id     TEXT         PRIMARY KEY,
                user_id              INTEGER      NOT NULL,
                name                 TEXT         NOT NULL,
                issuing_body         TEXT         NOT NULL,
                registration_number  TEXT         NOT NULL,
                jurisdiction         TEXT         NOT NULL,
                expires_at           TIMESTAMPTZ  NULL,
                verified_at          TIMESTAMPTZ  NULL,
                is_active            BOOLEAN      NOT NULL DEFAULT TRUE,
                created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS idx_qp_credentials_user_id
                       ON silver.qp_credentials (user_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_qp_credentials_verified
                       ON silver.qp_credentials (verified_at) WHERE verified_at IS NOT NULL');

        DB::statement('GRANT SELECT, INSERT, UPDATE, DELETE ON silver.qp_credentials TO georag_app');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS silver.qp_credentials CASCADE');
    }
};
