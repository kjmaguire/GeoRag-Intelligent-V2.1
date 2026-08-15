<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Perf fix (2026-08-15) — every `WHERE dp.embedding_id IS NULL` query in
 * app/hatchet_workflows/embed_pending_passages.py (the project-discovery
 * scan, the per-workspace pending gauge, the qdrant-drift healer's re-embed
 * re-resolve, and the ingest_progress completion sweep) runs a full seq
 * scan against silver.document_passages on every cron tick — there is no
 * supporting index, and the table only grows.
 *
 * The only existing related index, idx_document_passages_needs_enrichment
 * (2026_05_30_100000_add_contextualized_content_to_document_passages.php),
 * has predicate `contextualized_content IS NULL AND embedding_id IS NULL`.
 * That's strictly narrower than `embedding_id IS NULL` alone: for Postgres
 * to use a partial index, the query's WHERE clause must imply the index's
 * predicate, and "embedding_id IS NULL" does not imply anything about
 * contextualized_content — so the planner cannot use it for these queries.
 *
 * `CREATE INDEX CONCURRENTLY` cannot run inside a transaction block, hence
 * `$withinTransaction = false` — same pattern as
 * 2026_04_17_120000_drop_duplicate_gist_gin_indexes.php. That same
 * precedent also runs its CONCURRENTLY statements unconditionally with no
 * SQLite driver branch: the PHPUnit SQLite connection's global
 * `beforeExecuting` hook (tests/TestCase.php) no-ops every
 * `CREATE INDEX` / `DROP INDEX` statement outright ("indexes are not
 * needed for test correctness and many use PG-only syntax"), so this
 * migration is safe to run unmodified against both connections.
 */
return new class extends Migration
{
    public $withinTransaction = false;

    public function up(): void
    {
        DB::statement(
            'CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_document_passages_pending_embed '.
            'ON silver.document_passages (document_id) '.
            'WHERE embedding_id IS NULL',
        );
    }

    public function down(): void
    {
        DB::statement(
            'DROP INDEX CONCURRENTLY IF EXISTS silver.idx_document_passages_pending_embed',
        );
    }
};
