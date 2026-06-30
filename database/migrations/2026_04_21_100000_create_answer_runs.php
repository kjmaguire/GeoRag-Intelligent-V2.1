<?php

/**
 * B1.1 — Module 4 Phase B Chunk 1: Create silver.answer_runs.
 *
 * Date: 2026-04-21.
 *
 * Purpose
 * -------
 * Per-query audit trail for every RAG answer produced by the GeoRAG
 * deterministic orchestrator.  One row per answered query.  Captures:
 *   - The query text and spec-class routing decision (query_class)
 *   - Retrieval metadata: embedding + sparse model versions, fusion method,
 *     reranker version, retrieval strategy version (addendum §04h-i)
 *   - Data freshness at query time: workspace_data_version + project_data_version
 *     (addendum §05d — the cache-key authority)
 *   - LLM backend chain (Module 5 metadata)
 *   - Token accounting (prompt / completion / cache read / cache creation)
 *   - Citation lifecycle state (Module 6 drives the state machine)
 *   - OTel trace_id + root_span_id (addendum §07f)
 *
 * FK graph (parents must exist before this migration runs):
 *   answer_runs.workspace_id → silver.workspaces.workspace_id (CASCADE DELETE)
 *   answer_runs.project_id   → silver.projects.project_id     (SET NULL — answer
 *                              history survives project deletion for audit)
 *   answer_runs.user_id      → public.users.id (BIGINT, RESTRICT) — users table
 *                              confirmed present (0001_01_01_000000_create_users_table.php).
 *                              NULL for anonymous / system-initiated queries.
 *
 * ENUM vs CHECK
 * -------------
 * No PostgreSQL ENUM types.  All discriminators use VARCHAR + CHECK constraint,
 * matching the pattern established by evidence_items (migration 140000).
 *
 * Rollback: answer_retrieval_items (migration 110000) must be dropped first
 * (its own down() runs first in reverse-batch order).
 *
 * NOT in this migration
 * ---------------------
 * - answer_citation_items  (Module 6 scope — FKs answer_runs.answer_run_id)
 * - answer_retrieval_items (migration 110000 — separate file)
 * - Orchestrator INSERT wiring (Phase B Chunk 2)
 */

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    public function up(): void
    {
        // -----------------------------------------------------------------------
        // Create silver.answer_runs
        // -----------------------------------------------------------------------
        DB::statement(
            'CREATE TABLE IF NOT EXISTS silver.answer_runs (
                answer_run_id                    UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id                     UUID         NOT NULL
                    REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                project_id                       UUID         NULL
                    REFERENCES silver.projects(project_id) ON DELETE SET NULL,
                user_id                          BIGINT       NULL
                    REFERENCES public.users(id) ON DELETE RESTRICT,
                query_text                       TEXT         NOT NULL,
                query_class                      VARCHAR(32)  NOT NULL,

                -- Retrieval metadata (addendum §04h-i)
                embedding_model                  VARCHAR(128) NULL,
                embedding_model_version          VARCHAR(64)  NULL,
                sparse_model                     VARCHAR(128) NULL,
                sparse_model_version             VARCHAR(64)  NULL,
                fusion_method                    VARCHAR(16)  NULL,
                sparse_boost_applied             BOOLEAN      NULL,
                reranker_version                 VARCHAR(64)  NULL,
                retrieval_strategy_version       VARCHAR(32)  NULL,

                -- Freshness at query time (addendum §05d)
                workspace_data_version_at_query  BIGINT       NOT NULL,
                project_data_version_at_query    BIGINT       NULL,

                -- LLM backend (Module 5 metadata)
                backend_used                     VARCHAR(32)  NULL,
                backend_chain                    TEXT[]       NULL,
                model_name                       VARCHAR(128) NULL,
                input_tokens                     INTEGER      NULL,
                output_tokens                    INTEGER      NULL,
                cache_read_tokens                INTEGER      NULL,
                cache_creation_tokens            INTEGER      NULL,
                speculative_acceptance_rate_sample NUMERIC(6,4) NULL,
                evidence_truncated_count         INTEGER      NULL,

                -- Citation lifecycle (Module 6 drives the state machine)
                citation_lifecycle_state         VARCHAR(16)  NULL,
                citation_mode                    VARCHAR(32)  NULL,

                -- OTel (addendum §07f)
                trace_id                         VARCHAR(64)  NULL,
                root_span_id                     VARCHAR(32)  NULL,

                -- Timestamps
                created_at                       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at                       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

                CONSTRAINT answer_runs_pkey
                    PRIMARY KEY (answer_run_id),

                CONSTRAINT answer_runs_query_class_valid
                    CHECK (query_class IN (
                        \'factual\',
                        \'spatial\',
                        \'document\',
                        \'computation\',
                        \'viz\',
                        \'unknown\'
                    )),

                CONSTRAINT answer_runs_fusion_valid
                    CHECK (fusion_method IS NULL OR fusion_method IN (\'rrf\', \'dbsf\')),

                CONSTRAINT answer_runs_backend_valid
                    CHECK (backend_used IS NULL OR backend_used IN (\'vllm\', \'ollama\', \'anthropic\')),

                CONSTRAINT answer_runs_citation_state_valid
                    CHECK (citation_lifecycle_state IS NULL OR citation_lifecycle_state IN (
                        \'draft\',
                        \'generated\',
                        \'validated\',
                        \'committed\',
                        \'rejected\'
                    )),

                CONSTRAINT answer_runs_citation_mode_valid
                    CHECK (citation_mode IS NULL OR citation_mode IN (
                        \'posthoc_span_resolution\',
                        \'hybrid_delayed_attachment\'
                    ))
            )',
        );

        // -----------------------------------------------------------------------
        // Indices
        // -----------------------------------------------------------------------
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_answer_runs_workspace
                 ON silver.answer_runs (workspace_id)',
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_answer_runs_project
                 ON silver.answer_runs (project_id)',
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_answer_runs_created_at
                 ON silver.answer_runs (created_at DESC)',
        );

        // Partial index: only rows that carry an OTel trace (avoids indexing NULLs).
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_answer_runs_trace_id
                 ON silver.answer_runs (trace_id)
                 WHERE trace_id IS NOT NULL',
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_answer_runs_query_class
                 ON silver.answer_runs (query_class)',
        );
    }

    public function down(): void
    {
        // answer_retrieval_items (migration 110000) references answer_run_id.
        // Its down() runs first in reverse-batch order.  This down() only
        // drops the answer_runs table itself.
        DB::statement('DROP TABLE IF EXISTS silver.answer_runs');
    }
};
