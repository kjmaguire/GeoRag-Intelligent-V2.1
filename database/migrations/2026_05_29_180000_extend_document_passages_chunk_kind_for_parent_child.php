<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Plan §1b parent-child chunker — extend CHECK constraint.
 *
 * The original `document_passages_chunk_kind_check` (created before
 * the §1b spec) allowed:
 *   'narrative', 'table', 'caption_figure', 'character_window'
 *
 * The §1b chunker (commit 33bb26a) emits two new values:
 *   'section'    — parent passage holding the concatenated children
 *   'paragraph'  — child passage with parent_chunk_id FK to the section
 *
 * Without this migration, the `_insert_passages` SQL fails with a
 * CHECK constraint violation and silently degrades to 0 rows on
 * the parent + paragraph inserts (the catch handler logs +
 * continues), leaving the chunker's output discarded.
 *
 * Discovered 2026-05-29 during the end-to-end §1b+§3d verification
 * smoke. The unit tests in test_parent_child_chunker.py exercised
 * the in-memory dict shape but never hit a real DB, so the CHECK
 * mismatch slipped through.
 *
 * Idempotent: drops + recreates the constraint with the extended
 * value set.
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
                DROP CONSTRAINT IF EXISTS document_passages_chunk_kind_check
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE silver.document_passages
                ADD CONSTRAINT document_passages_chunk_kind_check
                CHECK (
                    chunk_kind IS NULL
                    OR chunk_kind::text = ANY (ARRAY[
                        'narrative'::varchar,
                        'table'::varchar,
                        'caption_figure'::varchar,
                        'character_window'::varchar,
                        'section'::varchar,
                        'paragraph'::varchar
                    ]::text[])
                )
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // The down path restores the pre-§1b value set. Any 'section' /
        // 'paragraph' rows would block the constraint creation; the
        // operator must manually migrate or delete them before running
        // down() in a context where they exist.
        DB::statement(<<<'SQL'
            ALTER TABLE silver.document_passages
                DROP CONSTRAINT IF EXISTS document_passages_chunk_kind_check
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE silver.document_passages
                ADD CONSTRAINT document_passages_chunk_kind_check
                CHECK (
                    chunk_kind IS NULL
                    OR chunk_kind::text = ANY (ARRAY[
                        'narrative'::varchar,
                        'table'::varchar,
                        'caption_figure'::varchar,
                        'character_window'::varchar
                    ]::text[])
                )
        SQL);
    }
};
