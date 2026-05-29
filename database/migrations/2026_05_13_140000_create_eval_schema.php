<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create `eval.*` schema — golden questions + eval run results
 * (doc-phase 97 / §10.1 + §10.5).
 *
 * Three tables per master-plan §24:
 *   - eval.golden_questions     — per-question definition with
 *                                  expected_citations, expected_entities,
 *                                  expected_numeric_values, etc.
 *   - eval.run_results          — one row per question per eval run
 *   - eval.run_summaries        — aggregate per-run pass/fail/regression
 *
 * Question sets per §24.1:
 *   core_chat | public_private_boundary | numeric_grounding |
 *   refusal_correctness | target_recommendation | report_section |
 *   schema_mapping | ocr_triage
 *
 * No RLS — eval content is GLOBAL operational data, not workspace-
 * scoped. Workspace-scoped eval would be a future overlay.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('CREATE SCHEMA IF NOT EXISTS eval;');
        DB::statement('SET search_path TO eval, silver, public;');

        // ---------------- golden_questions ----------------
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS eval.golden_questions (
                question_id      UUID         NOT NULL DEFAULT gen_random_uuid(),
                question_set     VARCHAR(40)  NOT NULL,
                question_text    TEXT         NOT NULL,
                context_setup    JSONB        NOT NULL DEFAULT '{}'::jsonb,
                expected_intent_class VARCHAR(60) NULL,
                expected_citations JSONB      NOT NULL DEFAULT '[]'::jsonb,
                expected_entities JSONB       NOT NULL DEFAULT '[]'::jsonb,
                expected_numeric_values JSONB NOT NULL DEFAULT '[]'::jsonb,
                expected_refusal BOOLEAN      NOT NULL DEFAULT false,
                expected_refusal_reason TEXT  NULL,
                expected_language_compliance JSONB NOT NULL DEFAULT '[]'::jsonb,
                difficulty       VARCHAR(10)  NOT NULL DEFAULT 'medium',
                authored_by_user_id BIGINT    NOT NULL,
                authored_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                reviewed_by_user_id BIGINT    NULL,
                reviewed_at      TIMESTAMPTZ  NULL,
                status           VARCHAR(10)  NOT NULL DEFAULT 'draft',
                CONSTRAINT golden_questions_pkey PRIMARY KEY (question_id),
                CONSTRAINT golden_questions_authored_by_fkey
                    FOREIGN KEY (authored_by_user_id)
                    REFERENCES public.users (id)
                    ON DELETE RESTRICT,
                CONSTRAINT golden_questions_reviewed_by_fkey
                    FOREIGN KEY (reviewed_by_user_id)
                    REFERENCES public.users (id)
                    ON DELETE SET NULL,
                CONSTRAINT golden_questions_set_valid
                    CHECK (question_set IN (
                        'core_chat', 'public_private_boundary', 'numeric_grounding',
                        'refusal_correctness', 'target_recommendation', 'report_section',
                        'schema_mapping', 'ocr_triage'
                    )),
                CONSTRAINT golden_questions_difficulty_valid
                    CHECK (difficulty IN ('easy', 'medium', 'hard')),
                CONSTRAINT golden_questions_status_valid
                    CHECK (status IN ('draft', 'active', 'retired'))
            );
        SQL);

        // ---------------- run_results ----------------
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS eval.run_results (
                result_id        UUID         NOT NULL DEFAULT gen_random_uuid(),
                run_id           UUID         NOT NULL,
                question_id      UUID         NOT NULL,
                passed           BOOLEAN      NOT NULL,
                actual_payload   JSONB        NOT NULL DEFAULT '{}'::jsonb,
                failure_layer    VARCHAR(40)  NULL,
                failure_detail   TEXT         NULL,
                latency_ms       INTEGER      NULL,
                tokens_used      INTEGER      NULL,
                executed_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT run_results_pkey PRIMARY KEY (result_id),
                CONSTRAINT run_results_question_id_fkey
                    FOREIGN KEY (question_id)
                    REFERENCES eval.golden_questions (question_id)
                    ON DELETE CASCADE,
                CONSTRAINT run_results_run_question_unique
                    UNIQUE (run_id, question_id)
            );
        SQL);

        // ---------------- run_summaries ----------------
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS eval.run_summaries (
                run_id           UUID         NOT NULL DEFAULT gen_random_uuid(),
                triggered_by     VARCHAR(40)  NOT NULL,
                trigger_payload  JSONB        NOT NULL DEFAULT '{}'::jsonb,
                question_set_filter VARCHAR(40) NULL,
                question_count   INTEGER      NOT NULL DEFAULT 0,
                pass_count       INTEGER      NOT NULL DEFAULT 0,
                fail_count       INTEGER      NOT NULL DEFAULT 0,
                regression_count INTEGER      NOT NULL DEFAULT 0,
                blocks_promotion BOOLEAN      NOT NULL DEFAULT false,
                promotion_override_by_user_id BIGINT NULL,
                promotion_override_reason TEXT NULL,
                started_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                completed_at     TIMESTAMPTZ  NULL,
                CONSTRAINT run_summaries_pkey PRIMARY KEY (run_id),
                CONSTRAINT run_summaries_triggered_by_valid
                    CHECK (triggered_by IN ('cron', 'manual', 'promotion_gate', 'prompt_change')),
                CONSTRAINT run_summaries_override_user_fkey
                    FOREIGN KEY (promotion_override_by_user_id)
                    REFERENCES public.users (id)
                    ON DELETE SET NULL,
                CONSTRAINT run_summaries_counts_consistent
                    CHECK (pass_count + fail_count <= question_count)
            );
        SQL);

        // ---------------- Indexes ----------------
        DB::statement('CREATE INDEX IF NOT EXISTS idx_golden_questions_set_status
                       ON eval.golden_questions (question_set, status);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_run_results_run
                       ON eval.run_results (run_id);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_run_results_question
                       ON eval.run_results (question_id);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_run_summaries_started
                       ON eval.run_summaries (started_at DESC);');

        // ---------------- Grants ----------------
        DB::statement('GRANT USAGE ON SCHEMA eval TO georag_app;');
        DB::statement('GRANT SELECT, INSERT, UPDATE, DELETE
                       ON ALL TABLES IN SCHEMA eval TO georag_app;');
        DB::statement('GRANT USAGE, SELECT
                       ON ALL SEQUENCES IN SCHEMA eval TO georag_app;');
    }

    public function down(): void
    {
        DB::statement('DROP SCHEMA IF EXISTS eval CASCADE;');
    }
};
