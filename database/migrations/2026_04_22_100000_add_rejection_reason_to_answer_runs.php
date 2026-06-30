<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Module 6 Phase B Chunk 3 — add rejection_reason to silver.answer_runs.
 *
 * Stores the human-readable guard-failure reason produced by evaluate_guards()
 * when any of the four §04i guards (numeric, entity, completeness, refusal)
 * triggers a 'rejected' lifecycle transition.
 *
 * Column spec:
 *   rejection_reason  TEXT NULL
 *     — NULL for non-rejected runs (committed / validated).
 *     — Populated on guard failure with a structured message naming which
 *       guards failed and a brief human-readable description.
 *     — No length limit: guard failure messages may concatenate multiple
 *       guard names + short excerpts and should not be silently truncated.
 *
 * The transition_lifecycle() helper in citation_lifecycle.py already
 * accepts rejection_reason and logs it; this migration adds the column
 * it will write to.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement(
            'ALTER TABLE silver.answer_runs
             ADD COLUMN IF NOT EXISTS rejection_reason TEXT NULL',
        );
    }

    public function down(): void
    {
        DB::statement(
            'ALTER TABLE silver.answer_runs
             DROP COLUMN IF EXISTS rejection_reason',
        );
    }
};
