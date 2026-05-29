<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Eval 09 P3 follow-up — persist §04i hallucination-guard outputs on the
 * answer_runs row.
 *
 * Why this column
 * ---------------
 * The hallucination-guard chain (Layer 2 coverage, L3 numeric, L4 refusal,
 * L5 conflict, L6 freshness) runs on every committed answer but its
 * outputs were ephemeral — only Prometheus counters and a stray "warnings"
 * list on the response object survived. Once an answer was streamed the
 * provenance was gone, which made:
 *   - HallucinationGuardBypassed alert tuning impossible (no way to tell
 *     "guards ran cleanly" from "guards never ran")
 *   - Post-incident review of a flagged answer manual & lossy
 *   - Periodic guard-effectiveness audits unfeasible
 *
 * JSONB shape (validated only at the read site, not by a DB CHECK so
 * future guard additions don't require a schema migration):
 *
 *   {
 *     "schema_version": 1,
 *     "guards": {
 *       "L2_coverage":  { "status": "pass|warn|fail", "score": 0.93, ... },
 *       "L3_numeric":   { "status": "pass", "violations": [] },
 *       "L4_refusal":   { "status": "pass" },
 *       "L5_conflict":  { "status": "pass|notice", "notices": [...] },
 *       "L6_freshness": { "status": "pass", "demoted_count": 2 }
 *     },
 *     "captured_at": "2026-05-20T18:00:00Z"
 *   }
 *
 * NULL means the guard chain did not run (cache short-circuit, or a
 * pre-guard error path). An empty `guards: {}` means the chain ran but
 * produced no entries — distinguishable from NULL by the orchestrator's
 * Prometheus label tuning (HallucinationGuardBypassed reads NULL as
 * "absent" and an empty object as "clean").
 *
 * GIN index on jsonb_path_ops keeps post-hoc audit queries fast without
 * paying the full B-tree cost of a generic ops class — we only ever
 * containment-query this column (`@>`), never range/equality on keys.
 *
 * SQLite (test DB) — gated on Postgres.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE silver.answer_runs
              ADD COLUMN IF NOT EXISTS hallucination_guard_results jsonb
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.answer_runs.hallucination_guard_results IS
              'Per-layer §04i guard chain outputs for this answer. NULL = chain did not run; {} = ran clean. Schema v1 (see migration 2026_05_20_020000).'
        SQL);

        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_answer_runs_guard_results_gin
              ON silver.answer_runs
              USING gin (hallucination_guard_results jsonb_path_ops)
              WHERE hallucination_guard_results IS NOT NULL
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement('DROP INDEX IF EXISTS silver.idx_answer_runs_guard_results_gin');
        DB::statement('ALTER TABLE silver.answer_runs DROP COLUMN IF EXISTS hallucination_guard_results');
    }
};
