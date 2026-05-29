<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Test-DB parity sibling for entity_aliases + alias_gaps.
 *
 * Per project_test_db_parity_gap memory. Shape-identical, no RLS, no
 * GRANT. Idempotent. NOT applied by overnight run.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.entity_aliases (
                alias_id          UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id      UUID         NOT NULL,
                entity_type       VARCHAR(40)  NOT NULL,
                canonical_name    VARCHAR(255) NOT NULL,
                canonical_uri     TEXT         NULL,
                alias             VARCHAR(255) NOT NULL,
                alias_normalised  VARCHAR(255) NOT NULL,
                confidence        NUMERIC(4,3) NOT NULL DEFAULT 1.000,
                source            VARCHAR(40)  NOT NULL DEFAULT 'sme',
                source_document_id UUID        NULL,
                evidence_text     TEXT         NULL,
                added_by_user_id  BIGINT       NULL,
                created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT entity_aliases_pkey PRIMARY KEY (alias_id),
                CONSTRAINT entity_aliases_workspace_alias_unique
                    UNIQUE (workspace_id, entity_type, alias_normalised)
            )
        SQL);

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.alias_gaps (
                gap_id            UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id      UUID         NOT NULL,
                entity_text       VARCHAR(255) NOT NULL,
                entity_text_normalised VARCHAR(255) NOT NULL,
                entity_type_guess VARCHAR(40)  NULL,
                query_id          UUID         NULL,
                user_id           BIGINT       NULL,
                detector          VARCHAR(40)  NOT NULL DEFAULT 'entity_resolver',
                created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                promoted_at       TIMESTAMPTZ  NULL,
                promoted_to_alias_id UUID      NULL,
                dismissed_at      TIMESTAMPTZ  NULL,
                dismissed_reason  VARCHAR(60)  NULL,
                CONSTRAINT alias_gaps_pkey PRIMARY KEY (gap_id)
            )
        SQL);
    }

    public function down(): void {}
};
