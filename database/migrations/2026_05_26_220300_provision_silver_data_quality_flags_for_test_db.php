<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Test-DB parity sibling for silver.data_quality_flags.
 *
 * See project_test_db_parity_gap memory: 120+ migrations that touch
 * raw-SQL tables need a paired provision sibling so the test DB tracks
 * production. Shape-identical CREATE IF NOT EXISTS, no GRANTs, no RLS
 * (test connection runs as owner).
 *
 * NOT applied by overnight run.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.data_quality_flags (
                flag_id            UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id       UUID         NOT NULL,
                project_id         UUID         NULL,
                record_type        VARCHAR(40)  NOT NULL,
                record_id          TEXT         NOT NULL,
                source_document_id UUID         NULL,
                source_page        INTEGER      NULL,
                source_row_range   TEXT         NULL,
                flag_type          VARCHAR(60)  NOT NULL,
                severity           VARCHAR(10)  NOT NULL,
                description        TEXT         NOT NULL,
                rule_id            VARCHAR(60)  NULL,
                rule_version       VARCHAR(20)  NULL,
                threshold_payload  JSONB        NOT NULL DEFAULT '{}'::jsonb,
                flagged_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                flagged_by         VARCHAR(40)  NOT NULL DEFAULT 'system',
                reviewed_by_user_id BIGINT      NULL,
                reviewed_at        TIMESTAMPTZ  NULL,
                resolved_at        TIMESTAMPTZ  NULL,
                resolution         VARCHAR(40)  NULL,
                resolution_notes   TEXT         NULL,
                CONSTRAINT data_quality_flags_pkey PRIMARY KEY (flag_id)
            )
        SQL);
    }

    public function down(): void {}
};
