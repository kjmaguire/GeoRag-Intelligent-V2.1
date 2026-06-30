<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * targeting.target_rationales — narrative rationale shape for the "Why this target?"
 * Foundry surface. Stores the evidence-stack, weighted factor list, analogue
 * comparisons, and confidence trajectory for a given target recommendation.
 *
 * Joined to targeting.target_recommendations via recommendation_id.
 */
return new class extends Migration
{
    public function up(): void
    {
        // Lives in silver.* (not targeting.*) because `georag_app` can't
        // create tables in the targeting schema. recommendation_id is a soft
        // reference to targeting.target_recommendations — no FK to keep this
        // migration runnable without superuser. Application-level validation
        // enforces the link.
        DB::statement('SET search_path TO silver, public;');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.target_rationales (
                rationale_id     UUID         NOT NULL DEFAULT gen_random_uuid(),
                recommendation_id UUID        NOT NULL,
                summary          TEXT         NULL,
                positives        JSONB        NOT NULL DEFAULT '[]'::jsonb,
                negatives        JSONB        NOT NULL DEFAULT '[]'::jsonb,
                analogues        JSONB        NOT NULL DEFAULT '[]'::jsonb,
                confidence_trajectory JSONB   NOT NULL DEFAULT '[]'::jsonb,
                citations        JSONB        NOT NULL DEFAULT '[]'::jsonb,
                alternates       JSONB        NOT NULL DEFAULT '[]'::jsonb,
                generated_by     VARCHAR(40)  NULL,
                created_at       TIMESTAMP(0) WITHOUT TIME ZONE,
                updated_at       TIMESTAMP(0) WITHOUT TIME ZONE,
                CONSTRAINT target_rationales_pkey PRIMARY KEY (rationale_id)
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS target_rationales_rec_idx ON silver.target_rationales (recommendation_id)');
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.target_rationales');
    }
};
