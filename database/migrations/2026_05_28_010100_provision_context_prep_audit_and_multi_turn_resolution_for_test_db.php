<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Test-DB sibling of 2026_05_28_010000 — adds the two new JSONB columns
 * to silver.query_traces on the test database when it's been provisioned
 * via the lightweight bootstrap (which doesn't carry the full migration
 * history from production).
 *
 * Pattern matches the test-DB parity convention in MEMORY:
 *   project_test_db_parity_gap_2026_05_25.md
 *
 * Idempotent (CREATE INDEX IF NOT EXISTS / ADD COLUMN IF NOT EXISTS),
 * so a test DB that already has the columns won't error.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // Only fire on the test DB to avoid double-running on production.
        $dbName = DB::selectOne('SELECT current_database() AS db')->db ?? '';
        if (! str_ends_with($dbName, '_test')) {
            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE silver.query_traces
                ADD COLUMN IF NOT EXISTS context_prep_audit JSONB
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE silver.query_traces
                ADD COLUMN IF NOT EXISTS multi_turn_resolution JSONB
        SQL);

        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_query_traces_context_prep_audit
                ON silver.query_traces USING GIN (context_prep_audit)
                WHERE context_prep_audit IS NOT NULL
        SQL);

        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_query_traces_multi_turn_resolution
                ON silver.query_traces USING GIN (multi_turn_resolution)
                WHERE multi_turn_resolution IS NOT NULL
        SQL);
    }

    public function down(): void
    {
        // No-op — the parent provision migration owns the column.
    }
};
