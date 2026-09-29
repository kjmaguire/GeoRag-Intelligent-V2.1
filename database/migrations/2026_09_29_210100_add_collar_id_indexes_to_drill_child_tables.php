<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * collar_id indexes on every table that hangs off silver.collars (§04e,
 * database audit 2026-09-29 PG-6).
 *
 * `foreignUuid()->references()` does not create an index, and nothing later
 * added one, so silver.samples, silver.lithology_logs and silver.surveys —
 * the three largest drill tables — had no index leading with collar_id. The
 * assay tool, the ingest_tabular replace (`DELETE ... WHERE collar_id =
 * ANY($1)`), desurvey, the strip-log reads and the mv_collar_summary refresh
 * all filter or join on it, so every per-hole read was a sequential scan.
 *
 * The typed tables from 2026_05_20_0603..0607 carry a (workspace_id,
 * collar_id) index, which cannot serve a lookup by collar_id alone. That
 * matters more now that 2026_09_29_210200 makes their FKs ON DELETE
 * CASCADE: the cascade issues `DELETE ... WHERE collar_id = $1` per deleted
 * collar, and those RI queries bypass RLS, so they carry no workspace_id
 * predicate for the composite index to use. Without these, deleting a
 * project with 2k holes is 2k sequential scans per table in one
 * transaction.
 *
 * silver.samples also gets the workspace_id index every sibling has: its
 * RLS predicate is `workspace_id = GUC`.
 *
 * CONCURRENTLY, so production writes are not blocked while each index
 * builds; that cannot run inside a transaction, hence $withinTransaction.
 * A CONCURRENTLY build that fails leaves an INVALID index behind, and
 * `IF NOT EXISTS` would then skip it forever — so an invalid leftover of
 * the same name is dropped first, which makes a re-run after a failure
 * finish the job instead of silently keeping a useless index.
 *
 * pgsql only: the SQLite test connection has none of these tables' real
 * shapes, and its beforeExecuting hook no-ops CREATE INDEX anyway.
 */
return new class extends Migration
{
    public $withinTransaction = false;

    /**
     * index name => [qualified table, column list].
     *
     * Depth columns trail collar_id where the per-hole reads order by them
     * (samples/lithology_logs by from_depth, surveys by depth). The newer
     * typed tables get a bare (collar_id) — their per-hole readers already
     * have a depth-ordered index or read through gold.
     *
     * @var array<string, array{0: string, 1: string}>
     */
    private const INDEXES = [
        'idx_samples_collar_depth' => ['silver.samples', 'collar_id, from_depth'],
        'idx_samples_workspace_id' => ['silver.samples', 'workspace_id'],
        'idx_lithology_logs_collar_depth' => ['silver.lithology_logs', 'collar_id, from_depth'],
        'idx_surveys_collar_depth' => ['silver.surveys', 'collar_id, depth'],
        'idx_geochemistry_collar_id' => ['silver.geochemistry', 'collar_id'],
        'idx_structure_collar_id' => ['silver.structure', 'collar_id'],
        'idx_alteration_collar_id' => ['silver.alteration', 'collar_id'],
        'idx_mineralization_collar_id' => ['silver.mineralization', 'collar_id'],
        'idx_recovery_collar_id' => ['silver.recovery', 'collar_id'],
        'idx_specific_gravity_collar_id' => ['silver.specific_gravity', 'collar_id'],
        'idx_geotechnical_collar_id' => ['silver.geotechnical', 'collar_id'],
        'idx_sample_intervals_collar_id' => ['silver.sample_intervals', 'collar_id'],
        'idx_assay_composites_collar_id' => ['gold.assay_composites', 'collar_id'],
        'idx_significant_intersections_collar_id' => ['gold.significant_intersections', 'collar_id'],
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (self::INDEXES as $index => [$table, $columns]) {
            if (! $this->tableExists($table)) {
                continue;
            }

            $schema = explode('.', $table, 2)[0];
            $this->dropIfInvalid($schema, $index);

            DB::statement("CREATE INDEX CONCURRENTLY IF NOT EXISTS {$index} ON {$table} ({$columns})");
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
