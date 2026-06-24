<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Extend eval.golden_questions.question_set CHECK to accept two
 * new buckets for the 2026-06-01 ChatGPT gap-question CSV import:
 *
 *   - gap_import_single_project  (500 per-project descriptive Qs)
 *   - gap_import_cross_project   (1000 A-vs-B comparison Qs)
 *
 * Keeps the imports filterable from the existing SME-authored sets
 * so they don't dilute baseline pass rates.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement(<<<'SQL'
            ALTER TABLE eval.golden_questions
                DROP CONSTRAINT IF EXISTS golden_questions_set_valid;
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE eval.golden_questions
                ADD CONSTRAINT golden_questions_set_valid
                CHECK (question_set IN (
                    'core_chat',
                    'public_private_boundary',
                    'numeric_grounding',
                    'refusal_correctness',
                    'target_recommendation',
                    'report_section',
                    'schema_mapping',
                    'ocr_triage',
                    'gap_import_single_project',
                    'gap_import_cross_project'
                ));
        SQL);
    }

    public function down(): void
    {
        DB::statement(<<<'SQL'
            DELETE FROM eval.golden_questions
            WHERE question_set IN (
                'gap_import_single_project',
                'gap_import_cross_project'
            );
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE eval.golden_questions
                DROP CONSTRAINT IF EXISTS golden_questions_set_valid;
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE eval.golden_questions
                ADD CONSTRAINT golden_questions_set_valid
                CHECK (question_set IN (
                    'core_chat',
                    'public_private_boundary',
                    'numeric_grounding',
                    'refusal_correctness',
                    'target_recommendation',
                    'report_section',
                    'schema_mapping',
                    'ocr_triage'
                ));
        SQL);
    }
};
