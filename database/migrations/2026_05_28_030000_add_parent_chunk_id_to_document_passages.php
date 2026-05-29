<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Plan §1b + §3d — silver.document_passages.parent_chunk_id.
 *
 * Self-referencing UUID pointing at a "parent" passage (typically the
 * section-level chunk containing a sub-chunk). Populated by the
 * Dagster ingest when section-aware chunking emits parent + child
 * passages from the same PDF section.
 *
 * §3d parent expansion (app/agent/parent_expansion.py): when a child
 * chunk wins retrieval, the expander fetches the parent and merges
 * the wider context — useful when the answer requires more than
 * the matched 200-token chunk.
 *
 * NULL on legacy rows + on flat-chunked passages with no parent.
 * The §3d expander treats NULL as "no parent; pass through".
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
                ADD COLUMN IF NOT EXISTS parent_chunk_id UUID
                    REFERENCES silver.document_passages(passage_id)
                    ON DELETE SET NULL
        SQL);

        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_document_passages_parent
                ON silver.document_passages (parent_chunk_id)
                WHERE parent_chunk_id IS NOT NULL
        SQL);

        // Provision on test DB too (idempotent).
        $dbName = DB::selectOne('SELECT current_database() AS db')->db ?? '';
        if (str_ends_with($dbName, '_test')) {
            // Same DDL covers both — no-op.
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }
        DB::statement('DROP INDEX IF EXISTS silver.idx_document_passages_parent');
        DB::statement('ALTER TABLE silver.document_passages DROP COLUMN IF EXISTS parent_chunk_id');
    }
};
