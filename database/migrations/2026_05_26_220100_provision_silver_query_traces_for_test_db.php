<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Test-DB parity sibling for 2026_05_26_220000_create_silver_query_traces.
 *
 * Per memory project_test_db_parity_gap.md, raw-SQL tables created in the
 * test database via Laravel migrations need an explicit provision sibling
 * that handles the test-DB-only schema/role/RLS bits if any differ from
 * production. For silver.query_traces the shape is identical to
 * production, so this sibling simply re-runs the CREATE TABLE IF NOT
 * EXISTS pattern on the test-DB connection and skips on non-pgsql
 * drivers. It also avoids the GRANT to georag_app which is a no-op on
 * the test DB (georag_test runs as the owner role).
 *
 * Idempotent. Safe to run after the production migration.
 *
 * NOT applied by this overnight run.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // Idempotent re-creation in case the test DB diverges or is
        // reset independently. CREATE TABLE IF NOT EXISTS is a no-op
        // when the production migration already ran in this connection.
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.query_traces (
                trace_id                    UUID         NOT NULL DEFAULT gen_random_uuid(),
                answer_run_id               UUID         NULL,
                workspace_id                UUID         NOT NULL,
                project_id                  UUID         NULL,
                user_id                     BIGINT       NULL,
                query_id                    UUID         NOT NULL,
                query_text                  TEXT         NOT NULL,
                normalized_query            TEXT         NULL,
                conversation_turn           INTEGER      NOT NULL DEFAULT 1,
                system_prompt_tokens        INTEGER      NULL,
                remaining_context_budget    INTEGER      NULL,
                final_token_count           INTEGER      NULL,
                router_decision             VARCHAR(64)  NULL,
                router_confidence           NUMERIC(4,3) NULL,
                effective_intent            VARCHAR(64)  NULL,
                otel_trace_id               VARCHAR(64)  NULL,
                qdrant_dense_count          INTEGER      NULL,
                qdrant_sparse_count         INTEGER      NULL,
                postgis_count               INTEGER      NULL,
                neo4j_count                 INTEGER      NULL,
                candidate_count_pre_rerank  INTEGER      NULL,
                selected_context_groups     INTEGER      NULL,
                guard_pass                  BOOLEAN      NULL,
                guard_failure_codes         TEXT[]       NULL,
                repair_attempts             SMALLINT     NOT NULL DEFAULT 0,
                death_loop_triggered        BOOLEAN      NOT NULL DEFAULT false,
                cache_hit                   BOOLEAN      NOT NULL DEFAULT false,
                cache_type                  VARCHAR(16)  NULL,
                latency_total_ms            INTEGER      NULL,
                latency_routing_ms          INTEGER      NULL,
                latency_retrieval_ms        INTEGER      NULL,
                latency_reranking_ms        INTEGER      NULL,
                latency_generation_ms       INTEGER      NULL,
                latency_guards_ms           INTEGER      NULL,
                trace_payload               JSONB        NOT NULL DEFAULT '{}'::jsonb,
                created_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT query_traces_pkey PRIMARY KEY (trace_id)
            )
        SQL);
    }

    public function down(): void
    {
        // Production migration's down() handles drop; this sibling is
        // idempotent and intentionally non-destructive on rollback.
    }
};
