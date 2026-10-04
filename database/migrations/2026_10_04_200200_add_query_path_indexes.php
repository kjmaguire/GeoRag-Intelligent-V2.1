<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Indexes for the hot query paths the 2026-10 database audit found scanning
 * (§04e, §06b -- indexes only, no RLS or data change).
 *
 *   created_at on silver.collars / samples / lithology_logs
 *     services/mv_refresh.py `_max_dependency_change` runs
 *     `SELECT MAX(created_at) FROM <table>` on each of the three on every
 *     ingestion event, to decide whether the mv_collar_summary refresh can be
 *     skipped. None had a created_at index, so the staleness check -- there to
 *     AVOID work -- was three sequential scans of the largest drill tables.
 *     With a btree on created_at the planner answers MAX() from one index end.
 *
 *   idx_document_passages_pending_created
 *     services/ingest/passage_embedder.py selects the un-embedded backlog with
 *     `WHERE embedding_id IS NULL ... ORDER BY dp.created_at ASC LIMIT n`, and
 *     RLS adds `workspace_id = <guc>`. Partial on the backlog, so it stays the
 *     size of the backlog rather than the table; (workspace_id, created_at,
 *     passage_id) is the filter, the order and a unique-ish tiebreak in one
 *     scan. Complements idx_document_passages_pending_embed (document_id),
 *     which serves the per-document lookups and cannot give this ordering.
 *
 *   idx_document_passages_text_trgm / idx_entity_aliases_norm_trgm
 *     services/qdrant_fallback.py (the Qdrant-outage lexical fallback) and
 *     agent/entity_resolver.py (fuzzy alias match) call pg_trgm. A function in
 *     the WHERE (`strict_word_similarity(...) > x`, `similarity(...) >= x`)
 *     cannot use an index, so both scanned every row they were allowed to see;
 *     the entity_resolver comment even claimed a trigram index existed. Both
 *     now use the indexable operators (`<<%`, `%`), and these are the GIN
 *     trigram indexes they need. WRITE COST: the document_passages one indexes
 *     the full text of every passage, so it is large and slows ingestion
 *     inserts; it is worth it only because the fallback runs when semantic
 *     search is already down. pg_trgm itself is created by
 *     2026_04_09_173750 / deploy/aws/bootstrap.sql; it is asserted here
 *     (IF NOT EXISTS) as 2026_05_23_120000 does.
 *
 * CONCURRENTLY, so production writes are not blocked while each index builds;
 * that cannot run inside a transaction, hence $withinTransaction. An invalid
 * leftover of the same name from a failed earlier build is dropped first, as in
 * 2026_09_29_210100, so a re-run finishes the job instead of `IF NOT EXISTS`
 * keeping a useless index forever.
 *
 * The audit-ledger index for external_notification is in
 * 2026_10_04_200300: audit.audit_ledger is partitioned and CONCURRENTLY is not
 * available on a partitioned parent.
 *
 * pgsql only.
 */
return new class extends Migration
{
    public $withinTransaction = false;

    /**
     * index name => [qualified table, definition after "ON <table> "].
     *
     * @var array<string, array{0: string, 1: string}>
     */
    private const INDEXES = [
        'idx_collars_created_at' => ['silver.collars', '(created_at DESC)'],
        'idx_samples_created_at' => ['silver.samples', '(created_at DESC)'],
        'idx_lithology_logs_created_at' => ['silver.lithology_logs', '(created_at DESC)'],
        'idx_document_passages_pending_created' => [
            'silver.document_passages',
            '(workspace_id, created_at, passage_id) WHERE embedding_id IS NULL',
        ],
        'idx_entity_aliases_norm_trgm' => ['silver.entity_aliases', 'USING gin (alias_normalised gin_trgm_ops)'],
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('CREATE EXTENSION IF NOT EXISTS pg_trgm');

        foreach (self::INDEXES as $index => [$table, $definition]) {
            if (! $this->tableExists($table)) {
                continue;
            }

            $schema = explode('.', $table, 2)[0];
            $this->dropIfInvalid($schema, $index);

            DB::statement("CREATE INDEX CONCURRENTLY IF NOT EXISTS {$index} ON {$table} {$definition}");
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (array_reverse(self::INDEXES, true) as $index => [$table]) {
            $schema = explode('.', $table, 2)[0];
            DB::statement("DROP INDEX CONCURRENTLY IF EXISTS {$schema}.{$index}");
        }
    }

    private function tableExists(string $qualified): bool
    {
        return DB::selectOne('SELECT to_regclass(?) IS NOT NULL AS present', [$qualified])->present;
    }

    private function dropIfInvalid(string $schema, string $index): void
    {
        $invalid = DB::selectOne(
            'SELECT NOT i.indisvalid AS invalid
               FROM pg_index i
               JOIN pg_class c ON c.oid = i.indexrelid
               JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = ? AND c.relname = ?',
            [$schema, $index],
        );

        if ($invalid !== null && $invalid->invalid) {
            DB::statement("DROP INDEX CONCURRENTLY IF EXISTS {$schema}.{$index}");
        }
    }
};
