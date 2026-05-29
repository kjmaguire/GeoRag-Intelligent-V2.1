<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Plan §0e — retrieval trace logging.
 *
 * Per-query structured trace, 1:1 with silver.answer_runs. The existing
 * answer_runs table captures the answer-side audit fields (embedding model
 * versions, reranker version, tokens, OTel trace_id, citation lifecycle).
 * This table captures the *retrieval-pipeline* fields plan §0e asks for
 * that answer_runs does NOT carry:
 *
 *   - normalized query + conversation turn
 *   - context budgeting (system prompt tokens, remaining budget)
 *   - router confidence + tool plan + tool calls
 *   - per-source retrieval candidate counts
 *   - reranker scores + dropped candidates
 *   - evidence types selected for context
 *   - guard results + failure codes + repair attempts + strategies
 *   - death loop detection
 *   - per-stage latency breakdown
 *
 * Stored as one JSONB blob (`trace_payload`) plus a handful of pulled-out
 * columns for the dashboard filters listed in plan §5c. The JSONB shape
 * is the verbatim plan §0e schema; columns are denormalised for query
 * speed only.
 *
 * RLS: enabled, scoped on workspace_id. Trace data leaks across
 * workspaces would be a bigger tenancy violation than answer data
 * because it contains tool filters and intermediate results.
 *
 * NOT applied by this overnight run — Kyle reviews and runs via
 *   php artisan migrate --database=pgsql_migrations
 * per memory project_pg_role_membership_gap_2026_05_22.md.
 */
return new class extends Migration
{
    public function up(): void
    {
        // sqlite tests skip cleanly — only the test_db has pgsql parity.
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.query_traces (
                trace_id                    UUID         NOT NULL DEFAULT gen_random_uuid(),
                answer_run_id               UUID         NULL
                    REFERENCES silver.answer_runs(answer_run_id) ON DELETE CASCADE,
                workspace_id                UUID         NOT NULL
                    REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                project_id                  UUID         NULL,
                user_id                     BIGINT       NULL
                    REFERENCES public.users(id) ON DELETE SET NULL,

                -- Query identification
                query_id                    UUID         NOT NULL,
                query_text                  TEXT         NOT NULL,
                normalized_query            TEXT         NULL,
                conversation_turn           INTEGER      NOT NULL DEFAULT 1,

                -- Context budgeting (plan §0b instrumentation)
                system_prompt_tokens        INTEGER      NULL,
                remaining_context_budget    INTEGER      NULL,
                final_token_count           INTEGER      NULL,

                -- Routing
                router_decision             VARCHAR(64)  NULL,
                router_confidence           NUMERIC(4,3) NULL,
                effective_intent            VARCHAR(64)  NULL,

                -- OTel pass-through (Q4 — denormalised from
                -- silver.answer_runs.trace_id so dashboards skip
                -- the FK join).
                otel_trace_id               VARCHAR(64)  NULL,

                -- Retrieval candidate counts per source (plan §0e
                -- raw_results_per_source). Denormalised from trace_payload
                -- for fast dashboard queries.
                qdrant_dense_count          INTEGER      NULL,
                qdrant_sparse_count         INTEGER      NULL,
                postgis_count               INTEGER      NULL,
                neo4j_count                 INTEGER      NULL,
                candidate_count_pre_rerank  INTEGER      NULL,
                selected_context_groups     INTEGER      NULL,

                -- Guard pass/fail summary (full breakdown in trace_payload)
                guard_pass                  BOOLEAN      NULL,
                guard_failure_codes         TEXT[]       NULL,
                repair_attempts             SMALLINT     NOT NULL DEFAULT 0,
                death_loop_triggered        BOOLEAN      NOT NULL DEFAULT false,

                -- Cache (plan §2h)
                cache_hit                   BOOLEAN      NOT NULL DEFAULT false,
                cache_type                  VARCHAR(16)  NULL,

                -- Latency totals (plan §5c SLA). Per-stage breakdown in
                -- trace_payload.latency_ms.
                latency_total_ms            INTEGER      NULL,
                latency_routing_ms          INTEGER      NULL,
                latency_retrieval_ms        INTEGER      NULL,
                latency_reranking_ms        INTEGER      NULL,
                latency_generation_ms       INTEGER      NULL,
                latency_guards_ms           INTEGER      NULL,

                -- Full plan-§0e trace object as JSONB. Verbatim shape;
                -- denormalised columns above are computed from this on
                -- write.
                trace_payload               JSONB        NOT NULL DEFAULT '{}'::jsonb,

                created_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

                CONSTRAINT query_traces_pkey
                    PRIMARY KEY (trace_id),

                CONSTRAINT query_traces_cache_type_valid
                    CHECK (cache_type IS NULL OR cache_type IN ('semantic', 'retrieval', 'miss'))
            )
        SQL);

        // Indexes — match the dashboard queries listed in plan §5c.
        DB::statement('CREATE INDEX IF NOT EXISTS idx_query_traces_workspace_created
                       ON silver.query_traces (workspace_id, created_at DESC)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_query_traces_answer_run
                       ON silver.query_traces (answer_run_id) WHERE answer_run_id IS NOT NULL');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_query_traces_latency
                       ON silver.query_traces (latency_total_ms DESC, created_at DESC)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_query_traces_guard_failures
                       ON silver.query_traces (created_at DESC) WHERE guard_pass = false');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_query_traces_death_loops
                       ON silver.query_traces (created_at DESC) WHERE death_loop_triggered = true');
        // OTel pass-through: partial index so the dashboard "join from
        // OTel trace_id back to the original query" is cheap. Most
        // rows will carry a trace_id; the partial WHERE saves NULL
        // index entries on the early traces from before OTel was wired.
        DB::statement('CREATE INDEX IF NOT EXISTS idx_query_traces_otel_trace_id
                       ON silver.query_traces (otel_trace_id)
                       WHERE otel_trace_id IS NOT NULL');
        // GIN index on the JSONB payload — supports ad-hoc queries on
        // the fields not denormalised above (e.g. vocab_terms_matched,
        // entities_resolved).
        DB::statement('CREATE INDEX IF NOT EXISTS idx_query_traces_payload_gin
                       ON silver.query_traces USING GIN (trace_payload jsonb_path_ops)');

        // ----------------------------------------------------------------
        // RLS — follows the canonical workspace-isolation pattern
        // established by 2026_05_25 RLS migrations.
        // ----------------------------------------------------------------
        DB::statement('ALTER TABLE silver.query_traces ENABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE silver.query_traces FORCE ROW LEVEL SECURITY');
        DB::statement(<<<'SQL'
            CREATE POLICY query_traces_workspace_isolation
                ON silver.query_traces
                USING (
                    workspace_id::text = current_setting('georag.workspace_id', true)
                )
                WITH CHECK (
                    workspace_id::text = current_setting('georag.workspace_id', true)
                )
        SQL);

        DB::statement('GRANT USAGE ON SCHEMA silver TO georag_app');
        DB::statement('GRANT SELECT, INSERT, UPDATE, DELETE
                       ON silver.query_traces TO georag_app');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS silver.query_traces CASCADE');
    }
};
