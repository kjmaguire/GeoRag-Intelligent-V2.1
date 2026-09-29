<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;

/**
 * §04e: answer_runs.citation_mode has one value, 'posthoc_span_resolution'
 * (SME-approved, Kyle, 2026-09-29).
 *
 * answer_runs_citation_mode_valid (2026_04_21_100000_create_answer_runs.php:
 * 132-136) also allowed 'hybrid_delayed_attachment', which nothing writes and
 * nothing configures — CLAUDE.md hard rule 4: "there is no best-effort
 * citation mode; citation_mode is always posthoc_span_resolution". The CHECK
 * now says the same thing. NULL stays allowed for historical rows written
 * before the column was populated.
 *
 * Should a historical row carry the retired value, the narrowed CHECK is
 * added NOT VALID — every new or updated row is held to it, the historical
 * row is kept as recorded, and a warning names the count. It is never
 * rewritten: that would falsify what the run actually did.
 *
 * The Pydantic side is CitationModeLiteral in
 * src/fastapi/app/models/answer_run.py, narrowed in the same change.
 */
return new class extends Migration
{
    private const CONSTRAINT = 'answer_runs_citation_mode_valid';

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        $retired = (int) DB::scalar(
            "SELECT count(*) FROM silver.answer_runs
              WHERE citation_mode IS NOT NULL AND citation_mode <> 'posthoc_span_resolution'",
        );

        DB::statement('ALTER TABLE silver.answer_runs DROP CONSTRAINT IF EXISTS '.self::CONSTRAINT);
        DB::statement(
            'ALTER TABLE silver.answer_runs ADD CONSTRAINT '.self::CONSTRAINT
            ." CHECK (citation_mode IS NULL OR citation_mode = 'posthoc_span_resolution')"
            .($retired > 0 ? ' NOT VALID' : ''),
        );

        if ($retired > 0) {
            Log::warning('answer_runs_citation_mode_valid added NOT VALID: historical runs with a retired citation_mode kept', [
                'rows' => $retired,
            ]);
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('ALTER TABLE silver.answer_runs DROP CONSTRAINT IF EXISTS '.self::CONSTRAINT);
        DB::statement(
            'ALTER TABLE silver.answer_runs ADD CONSTRAINT '.self::CONSTRAINT
            ." CHECK (citation_mode IS NULL OR citation_mode IN ('posthoc_span_resolution', 'hybrid_delayed_attachment'))",
        );
    }
};
