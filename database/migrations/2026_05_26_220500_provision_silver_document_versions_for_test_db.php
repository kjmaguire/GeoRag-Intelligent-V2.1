<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Test-DB parity sibling for silver.document_versions.
 *
 * Per project_test_db_parity_gap memory. Shape-identical, no RLS, no
 * GRANT. Idempotent.
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
            CREATE TABLE IF NOT EXISTS silver.document_versions (
                version_id         UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id       UUID         NOT NULL,
                project_id         UUID         NULL,
                document_id        UUID         NOT NULL,
                superseded_by_id   UUID         NULL,
                report_type        VARCHAR(60)  NOT NULL,
                version_number     INTEGER      NOT NULL DEFAULT 1,
                effective_date     DATE         NULL,
                effective_date_source VARCHAR(40) NULL,
                is_current         BOOLEAN      NOT NULL DEFAULT true,
                property_id        UUID         NULL,
                property_name      VARCHAR(255) NULL,
                superseded_at      TIMESTAMPTZ  NULL,
                supersession_reason VARCHAR(255) NULL,
                superseded_by_event VARCHAR(40) NULL,
                created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT document_versions_pkey PRIMARY KEY (version_id),
                CONSTRAINT document_versions_document_unique UNIQUE (document_id)
            )
        SQL);
    }

    public function down(): void {}
};
