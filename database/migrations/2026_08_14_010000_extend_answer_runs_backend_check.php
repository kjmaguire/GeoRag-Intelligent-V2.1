<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * The backend_used CHECK was frozen at the vLLM-cutover set
 * ('vllm', 'anthropic', 'unknown') by 2026_06_02_220000, but the Azure
 * lift (Phase C, 2026-07-30) made LLM_BACKEND=azure the live default —
 * a value the constraint rejects. The agentic persist path never wrote
 * backend_used at all (silent NULL), so the violation stayed latent
 * until the column was wired (RAG-quality audit 2026-08-14, finding 5).
 * Extends the set with 'azure'; the FastAPI BackendLiteral in
 * src/fastapi/app/models/answer_run.py mirrors this list and a contract
 * test parses this file's VALUES constant to keep the two in lockstep.
 */
return new class extends Migration
{
    private const VALUES = "'vllm', 'anthropic', 'azure', 'unknown'";

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return; // sqlite test DB carries no CHECK for this column
        }

        DB::statement('ALTER TABLE silver.answer_runs DROP CONSTRAINT IF EXISTS answer_runs_backend_valid');
        DB::statement(
            'ALTER TABLE silver.answer_runs ADD CONSTRAINT answer_runs_backend_valid '
            .'CHECK (backend_used IS NULL OR backend_used IN ('.self::VALUES.'))',
        );
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('ALTER TABLE silver.answer_runs DROP CONSTRAINT IF EXISTS answer_runs_backend_valid');
        DB::statement(
            'ALTER TABLE silver.answer_runs ADD CONSTRAINT answer_runs_backend_valid '
            ."CHECK (backend_used IS NULL OR backend_used IN ('vllm', 'anthropic', 'unknown'))",
        );
    }
};
