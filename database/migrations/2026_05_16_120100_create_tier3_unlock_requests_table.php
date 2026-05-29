<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * silver.tier3_unlock_requests — workspace requests for jurisdiction-gated
 * Tier-3 PGEO layers. Tracks selected layers + 3 attestations + admin
 * approval state.
 */
return new class extends Migration {
    public function up(): void
    {
        DB::statement('SET search_path TO silver, public;');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.tier3_unlock_requests (
                request_id       UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id     UUID         NOT NULL,
                requested_by     BIGINT       NOT NULL,
                layer_ids        TEXT[]       NOT NULL DEFAULT '{}',
                attest_purpose       BOOLEAN  NOT NULL DEFAULT false,
                attest_retention     BOOLEAN  NOT NULL DEFAULT false,
                attest_attribution   BOOLEAN  NOT NULL DEFAULT false,
                status           VARCHAR(20)  NOT NULL DEFAULT 'pending',
                reviewed_by      BIGINT       NULL,
                reviewed_at      TIMESTAMP(0) WITHOUT TIME ZONE NULL,
                review_note      TEXT         NULL,
                created_at       TIMESTAMP(0) WITHOUT TIME ZONE,
                updated_at       TIMESTAMP(0) WITHOUT TIME ZONE,
                CONSTRAINT tier3_unlock_requests_pkey PRIMARY KEY (request_id),
                CONSTRAINT tier3_unlock_requests_status_valid
                    CHECK (status IN ('pending', 'approved', 'denied'))
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS tier3_unlock_workspace_idx ON silver.tier3_unlock_requests (workspace_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS tier3_unlock_status_idx ON silver.tier3_unlock_requests (status)');
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.tier3_unlock_requests');
    }
};
