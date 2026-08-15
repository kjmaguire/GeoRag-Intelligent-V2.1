<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * DB dimension push-to-9.5 sweep (2026-08-15) — extends the M1 autovacuum
 * tuning (2026_08_14_020100_tune_autovacuum_for_churn_tables.php, applied
 * to silver.ingest_progress + silver.document_passages) to the two other
 * high-churn tables the same audit flagged but didn't yet cover:
 *
 *   silver.review_queue — every lifecycle transition (pending ->
 *                         in_review -> decided -> committed -> archived)
 *                         is an UPDATE against a row that was already
 *                         written once at ingest time; confidence_per_field
 *                         / outlier_flags / decision_payload churn on the
 *                         same row across a review's lifetime.
 *   silver.answer_runs  — every RAG query INSERTs a row and then UPDATEs
 *                         it multiple times as the pipeline progresses
 *                         (plan_json backfill in decomposition.py,
 *                         two separate UPDATEs in citation_lifecycle.py,
 *                         plus the partial-update payload path in
 *                         models/answer_run.py) — this is the highest
 *                         query-volume table in the schema (one row
 *                         family per chat turn) and was previously
 *                         missing from the tuned set entirely.
 *
 * Same values as the M1 migration for consistency: autovacuum visits
 * ~4-5x more often than the 20%/10% global defaults, bounding bloat and
 * keeping planner stats fresh for the review-queue dashboard and
 * answer-quality reporting paths.
 *
 * ALTER TABLE ... SET (storage parameters) is idempotent (brief ACCESS
 * EXCLUSIVE metadata lock, no table rewrite). Skipped on sqlite.
 */
return new class extends Migration
{
    private const TABLES = [
        'silver.review_queue',
        'silver.answer_runs',
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (self::TABLES as $table) {
            DB::statement(<<<SQL
                ALTER TABLE {$table} SET (
                    autovacuum_vacuum_scale_factor = 0.05,
                    autovacuum_analyze_scale_factor = 0.02
                )
            SQL);
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (self::TABLES as $table) {
            DB::statement(<<<SQL
                ALTER TABLE {$table} RESET (
                    autovacuum_vacuum_scale_factor,
                    autovacuum_analyze_scale_factor
                )
            SQL);
        }
    }
};
