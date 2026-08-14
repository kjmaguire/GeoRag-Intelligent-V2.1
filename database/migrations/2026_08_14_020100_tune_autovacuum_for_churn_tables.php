<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * 2026-08-14 DB audit item M1 — per-table autovacuum tuning for the two
 * highest-churn tables:
 *
 *   silver.ingest_progress    — every workflow step UPDATEs its run row
 *                               (heartbeats every 30s per active ingest),
 *                               so dead tuples accumulate far faster than
 *                               row count grows.
 *   silver.document_passages  — bulk INSERT on ingest + per-row UPDATE
 *                               when the embed sweep back-fills
 *                               embedding_id / contextualized_content.
 *
 * The global defaults (vacuum at 20% dead, analyze at 10% change) let
 * bloat and stale planner stats build up for hours on these tables.
 * 0.05 / 0.02 makes autovacuum visit them roughly 4-5x as often, which
 * both bounds bloat and keeps the partial-index/DISTINCT ON plans the
 * IngestionRuns UI relies on fresh.
 *
 * ALTER TABLE ... SET (storage parameters) is idempotent by nature —
 * re-running simply re-asserts the same reloptions. Takes a brief
 * ACCESS EXCLUSIVE lock but no table rewrite. Skipped on sqlite
 * (silver.* never exists there).
 */
return new class extends Migration
{
    private const TABLES = [
        'silver.ingest_progress',
        'silver.document_passages',
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
