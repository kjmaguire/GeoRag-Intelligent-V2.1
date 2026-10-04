<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Drop the bare (collar_id) indexes on the three interval tables that
 * 2026_10_04_100000 gave a (collar_id, source_file) index
 * (database audit 2026-10).
 *
 * 2026_09_29_210100 created idx_structure_collar_id, idx_alteration_collar_id and
 * idx_mineralization_collar_id on (collar_id). 2026_10_04_100000 then created
 * idx_{structure,alteration,mineralization}_collar_source_file on
 * (collar_id, source_file). A btree on (a, b) serves every lookup a btree on (a)
 * does -- (collar_id) is a leading prefix -- so the single-column indexes now cost
 * write amplification on every interval insert and the disk and cache they take,
 * for no read benefit. The FK-cascade lookup (DELETE FROM silver.collars) that
 * 210100 added them for is served by the leading column of the composite.
 *
 * Only these three: surveys / lithology_logs / samples keep their
 * (collar_id, <depth>) indexes (different second column, ordering the per-hole
 * reads), and the other 210100 tables have no composite. The (workspace_id,
 * collar_id) originals from 2026_05_20_060400 lead with workspace_id and are not
 * redundant for a collar_id-only predicate.
 *
 * DROP INDEX CONCURRENTLY, hence $withinTransaction = false. down() recreates the
 * indexes CONCURRENTLY (an INVALID leftover of the same name is dropped first).
 * pgsql only; a table or composite index that is absent on this cluster leaves the
 * bare index alone -- never drop the only index covering collar_id.
 */
return new class extends Migration
{
    public $withinTransaction = false;

    /**
     * bare index => [qualified table, composite index that supersedes it].
     *
     * @var array<string, array{0: string, 1: string}>
     */
    private const INDEXES = [
        'idx_structure_collar_id' => ['silver.structure', 'idx_structure_collar_source_file'],
        'idx_alteration_collar_id' => ['silver.alteration', 'idx_alteration_collar_source_file'],
        'idx_mineralization_collar_id' => ['silver.mineralization', 'idx_mineralization_collar_source_file'],
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (self::INDEXES as $index => [$table, $composite]) {
            if (! $this->tableExists($table)) {
                continue;
            }

            $schema = explode('.', $table, 2)[0];

            // Only drop once the superseding index is there and usable.
            if (! $this->validIndexExists($schema, $composite)) {
                continue;
            }

            DB::statement("DROP INDEX CONCURRENTLY IF EXISTS {$schema}.{$index}");
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (self::INDEXES as $index => [$table]) {
            if (! $this->tableExists($table)) {
                continue;
            }

            $schema = explode('.', $table, 2)[0];

            if ($this->indexExists($schema, $index) && ! $this->validIndexExists($schema, $index)) {
                DB::statement("DROP INDEX CONCURRENTLY IF EXISTS {$schema}.{$index}");
            }

            DB::statement("CREATE INDEX CONCURRENTLY IF NOT EXISTS {$index} ON {$table} (collar_id)");
        }
    }

    private function tableExists(string $qualified): bool
    {
        return DB::selectOne('SELECT to_regclass(?) IS NOT NULL AS present', [$qualified])->present;
    }

    private function indexExists(string $schema, string $index): bool
    {
        return DB::selectOne('SELECT to_regclass(?) IS NOT NULL AS present', ["{$schema}.{$index}"])->present;
    }

    private function validIndexExists(string $schema, string $index): bool
    {
        $row = DB::selectOne(
            'SELECT i.indisvalid AS valid
               FROM pg_index i
               JOIN pg_class c ON c.oid = i.indexrelid
               JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = ? AND c.relname = ?',
            [$schema, $index],
        );

        return $row !== null && $row->valid;
    }
};
