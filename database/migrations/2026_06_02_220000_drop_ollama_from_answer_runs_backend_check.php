<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Tighten every backend-allowlist CHECK constraint to reflect the
 * completed vLLM cutover.
 *
 * Per CLAUDE.md "Technology snapshot": Ollama is sunset — vLLM serves
 * both dev and prod, Anthropic is the optional fallback. Two tables
 * still admit 'ollama' as a legal value, meaning a stale code path
 * could silently write `backend='ollama'` and pass schema validation
 * despite the backend being unreachable:
 *
 *   silver.answer_runs.backend_used        — constraint answer_runs_backend_valid
 *   silver.assessment_report_summaries.model_backend — constraint chk_assessment_summary_backend
 *
 * Audit pass 1 caught only the first; audit pass 3 caught the second.
 * Both get tightened here in one atomic migration so the cutover is
 * either fully applied or fully rolled back.
 *
 * 'unknown' is added to the answer_runs allow-list so the persist path
 * always has a safe fallback when backend detection fails (preferable
 * to NULL because it can be queried + alerted on). The summaries table
 * doesn't need 'unknown' — model_backend is NOT NULL and always set by
 * the VL generator.
 *
 * Constraint names preserved verbatim. Down() restores the original
 * allow-lists for clean rollback.
 *
 * Audit reference: P4-C in docs/handover/AUDIT_AND_FIX_REPORT.md.
 */
return new class extends Migration
{
    public function up(): void
    {
        // silver.answer_runs
        DB::statement('ALTER TABLE silver.answer_runs DROP CONSTRAINT IF EXISTS answer_runs_backend_valid');
        DB::statement(
            'ALTER TABLE silver.answer_runs ADD CONSTRAINT answer_runs_backend_valid'
            ." CHECK (backend_used IS NULL OR backend_used IN ('vllm', 'anthropic', 'unknown'))",
        );

        // silver.assessment_report_summaries
        DB::statement('ALTER TABLE silver.assessment_report_summaries DROP CONSTRAINT IF EXISTS chk_assessment_summary_backend');
        DB::statement(
            'ALTER TABLE silver.assessment_report_summaries ADD CONSTRAINT chk_assessment_summary_backend'
            ." CHECK (model_backend IN ('vllm', 'anthropic'))",
        );
    }

    public function down(): void
    {
        DB::statement('ALTER TABLE silver.answer_runs DROP CONSTRAINT IF EXISTS answer_runs_backend_valid');
        DB::statement(
            'ALTER TABLE silver.answer_runs ADD CONSTRAINT answer_runs_backend_valid'
            ." CHECK (backend_used IS NULL OR backend_used IN ('vllm', 'ollama', 'anthropic'))",
        );

        DB::statement('ALTER TABLE silver.assessment_report_summaries DROP CONSTRAINT IF EXISTS chk_assessment_summary_backend');
        DB::statement(
            'ALTER TABLE silver.assessment_report_summaries ADD CONSTRAINT chk_assessment_summary_backend'
            ." CHECK (model_backend IN ('vllm', 'anthropic', 'ollama'))",
        );
    }
};
