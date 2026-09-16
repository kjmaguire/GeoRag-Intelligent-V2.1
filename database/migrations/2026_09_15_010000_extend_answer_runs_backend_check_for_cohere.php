<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * ADR-0023 moved chat off Amazon Bedrock onto Cohere's own API and made
 * `LLM_BACKEND=cohere` the default. The CHECK set last moved on 2026-09-08
 * and stops at 'bedrock'.
 *
 * This is the same defect the 2026-09-08 migration was written for, one
 * backend later, and it is worth restating because it does NOT fail loudly.
 * `normalize_backend()` maps anything outside its known set to 'unknown'
 * precisely so an unrecognised backend can never violate the constraint — so
 * the symptom is not a CheckViolationError, it is every answer run recording
 * `backend_used = 'unknown'`. A column full of no information, with nothing
 * raised anywhere.
 *
 * 'azure' and 'bedrock' are both KEPT. The constraint governs rows already
 * written as well as rows still to come. 'azure' can no longer be selected at
 * all (Settings rejects it outright); 'bedrock' still can, and remains a
 * supported backend for an operator who does deploy the Marketplace endpoint
 * — it simply stopped being the default.
 *
 * The FastAPI BackendLiteral in src/fastapi/app/models/answer_run.py mirrors
 * this list, and tests/test_backend_enum_contract.py parses the VALUES
 * constant below to hold the two in lockstep — including a check that the
 * CONFIGURED DEFAULT backend is representable, which is what catches this
 * class of drift rather than the agreement check alone.
 */
return new class extends Migration
{
    private const VALUES = "'vllm', 'anthropic', 'azure', 'bedrock', 'cohere', 'unknown'";

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
            ."CHECK (backend_used IS NULL OR backend_used IN ('vllm', 'anthropic', 'azure', 'bedrock', 'unknown'))",
        );
    }
};
