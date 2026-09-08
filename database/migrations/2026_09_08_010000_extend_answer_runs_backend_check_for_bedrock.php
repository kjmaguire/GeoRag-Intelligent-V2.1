<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * ADR-0022 moved the LLM off Azure AI Foundry onto Amazon Bedrock and made
 * `LLM_BACKEND=bedrock` the default. The CHECK set last moved on 2026-08-14
 * and stops at 'azure'.
 *
 * This one does NOT fail loudly, which is why it needs writing down.
 * `normalize_backend()` maps anything outside its known set to 'unknown'
 * precisely so an unrecognised backend can never violate the constraint —
 * so the symptom is not a CheckViolationError, it is every answer run on
 * AWS recording `backend_used = 'unknown'`. That is the exact silent-NULL
 * class of defect the 2026-08-14 audit (finding 5) wired the column to
 * remove, arriving back through a different door.
 *
 * 'azure' is KEPT. The constraint governs rows already written as well as
 * rows still to come, and a value that was live for six weeks stays legal
 * even though `Settings` now rejects `LLM_BACKEND=azure` outright, so no
 * new row can carry it.
 *
 * The FastAPI BackendLiteral in src/fastapi/app/models/answer_run.py
 * mirrors this list, and tests/test_backend_enum_contract.py parses the
 * VALUES constant below to hold the two in lockstep.
 */
return new class extends Migration
{
    private const VALUES = "'vllm', 'anthropic', 'azure', 'bedrock', 'unknown'";

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
            ."CHECK (backend_used IS NULL OR backend_used IN ('vllm', 'anthropic', 'azure', 'unknown'))",
        );
    }
};
