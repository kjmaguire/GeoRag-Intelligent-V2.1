<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Test-DB parity migration — add silver.document_passages.chunk_kind.
 *
 * The parent_child_chunker_spec.md states "the existing chunk_kind column
 * accepts arbitrary strings... No schema migration required" — but no
 * Laravel migration in the chain ever adds the column. It exists on
 * production because it was patched in out-of-band (likely via a §10p
 * raw-SQL step that never got mirrored into a Laravel migration).
 *
 * The 2026_05_29_180000_extend_document_passages_chunk_kind_for_parent_child
 * migration that follows assumes the column exists; on a fresh test DB
 * (phpunit.pgsql.xml) the column does not, so ADD CONSTRAINT fails with
 * `column "chunk_kind" does not exist`, which in turn breaks every
 * RefreshDatabase test (observed 2026-05-28 on tests/Feature/Tenancy/*).
 *
 * Per the test-DB parity convention (see memory/project_test_db_parity_gap.md)
 * this is an idempotent ADD COLUMN IF NOT EXISTS so it is a no-op on prod
 * (where the column already exists) and provisions the column on the
 * test DB. Runs ahead of 2026_05_29_180000 by timestamp.
 *
 * Column shape mirrors how the dagster + fastapi writers use it
 * (varchar of short literal values: 'narrative', 'table', 'caption_figure',
 * 'character_window', 'section', 'paragraph', 'structured_summary').
 * `parser_used` is the sibling column used by the ADR-0012 structured
 * summary asset and has the same provenance gap; included here to keep
 * the test DB schema fully aligned with the writers.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE silver.document_passages
                ADD COLUMN IF NOT EXISTS chunk_kind  varchar(50),
                ADD COLUMN IF NOT EXISTS parser_used varchar(50)
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // Down path drops both columns. The sibling
        // 2026_05_29_180000 down() removes the CHECK constraint that
        // references chunk_kind first (laravel runs migrations in
        // reverse-timestamp order on rollback), so this is safe.
        DB::statement(<<<'SQL'
            ALTER TABLE silver.document_passages
                DROP COLUMN IF EXISTS chunk_kind,
                DROP COLUMN IF EXISTS parser_used
        SQL);
    }
};
