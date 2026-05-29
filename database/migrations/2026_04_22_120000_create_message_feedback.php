<?php

declare(strict_types=1);

/**
 * Module 7 Phase B Chunk 1 — Create silver.message_feedback.
 *
 * Date: 2026-04-22.
 *
 * Purpose
 * -------
 * Per-answer thumbs-up / thumbs-down feedback from authenticated users.
 * One row per feedback submission.  Multiple rows per user per answer_run are
 * allowed (user can change their mind); the UI renders the latest row at
 * render time (ORDER BY created_at DESC LIMIT 1 per user).
 *
 * Polarity taxonomy
 * -----------------
 *   'up'   — positive signal; category is optional (nullable)
 *   'down' — negative signal; category is required (CHECK constraint)
 *
 * Category taxonomy (§10p — 6 values, stable across Module 7)
 * -----------------------------------------------------------
 *   'hallucinated'   — model invented / fabricated facts
 *   'wrong_facts'    — factually incorrect but plausible
 *   'missing_info'   — incomplete answer / omitted relevant context
 *   'off_topic'      — answer not relevant to the question
 *   'citation_issue' — wrong, missing, or mis-matched citations
 *   'length_issue'   — answer too long or too short
 *
 * FK graph (parents must pre-exist)
 * ----------------------------------
 *   message_feedback.answer_run_id → silver.answer_runs.answer_run_id (CASCADE DELETE)
 *   message_feedback.workspace_id  → silver.workspaces.workspace_id   (CASCADE DELETE)
 *   message_feedback.user_id       → public.users.id                  (SET NULL on delete)
 *
 * CHECK constraints
 * -----------------
 *   message_feedback_polarity_valid           — polarity IN ('up', 'down')
 *   message_feedback_category_required_when_down
 *                                             — polarity = 'up' OR category IS NOT NULL
 *   message_feedback_category_valid           — category IS NULL OR category IN (6 values)
 *
 * Rollback
 * --------
 * down() drops all 6 indexes and the table.  No downstream tables reference
 * message_feedback so CASCADE is not needed.
 */

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    public function up(): void
    {
        DB::statement(
            'CREATE TABLE IF NOT EXISTS silver.message_feedback (
                feedback_id   UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
                answer_run_id UUID         NOT NULL
                    REFERENCES silver.answer_runs(answer_run_id) ON DELETE CASCADE,
                workspace_id  UUID         NOT NULL
                    REFERENCES silver.workspaces(workspace_id)   ON DELETE CASCADE,
                user_id       BIGINT       NULL
                    REFERENCES public.users(id) ON DELETE SET NULL,
                polarity      VARCHAR(8)   NOT NULL,
                category      VARCHAR(32)  NULL,
                note          TEXT         NULL,
                created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT message_feedback_polarity_valid
                    CHECK (polarity IN (\'up\', \'down\')),
                CONSTRAINT message_feedback_category_required_when_down
                    CHECK (polarity = \'up\' OR (polarity = \'down\' AND category IS NOT NULL)),
                CONSTRAINT message_feedback_category_valid
                    CHECK (category IS NULL OR category IN (
                        \'hallucinated\',
                        \'wrong_facts\',
                        \'missing_info\',
                        \'off_topic\',
                        \'citation_issue\',
                        \'length_issue\'
                    ))
            )'
        );

        DB::statement(
            'CREATE INDEX idx_message_feedback_answer_run
             ON silver.message_feedback (answer_run_id)'
        );

        DB::statement(
            'CREATE INDEX idx_message_feedback_workspace
             ON silver.message_feedback (workspace_id)'
        );

        DB::statement(
            'CREATE INDEX idx_message_feedback_user
             ON silver.message_feedback (user_id)
             WHERE user_id IS NOT NULL'
        );

        DB::statement(
            'CREATE INDEX idx_message_feedback_created_at
             ON silver.message_feedback (created_at DESC)'
        );

        DB::statement(
            'CREATE INDEX idx_message_feedback_polarity
             ON silver.message_feedback (polarity)'
        );

        DB::statement(
            'CREATE INDEX idx_message_feedback_category
             ON silver.message_feedback (category)
             WHERE category IS NOT NULL'
        );
    }

    public function down(): void
    {
        DB::statement('DROP INDEX IF EXISTS silver.idx_message_feedback_category');
        DB::statement('DROP INDEX IF EXISTS silver.idx_message_feedback_polarity');
        DB::statement('DROP INDEX IF EXISTS silver.idx_message_feedback_created_at');
        DB::statement('DROP INDEX IF EXISTS silver.idx_message_feedback_user');
        DB::statement('DROP INDEX IF EXISTS silver.idx_message_feedback_workspace');
        DB::statement('DROP INDEX IF EXISTS silver.idx_message_feedback_answer_run');
        DB::statement('DROP TABLE IF EXISTS silver.message_feedback');
    }
};
