<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Two index-hygiene fixes found by the 2026-10 database review, both on the
 * live migrated schema (migrations + the db:apply-raw manifest).
 *
 * ## 1. Foreign-key columns the project-delete path scans sequentially
 *
 * PostgreSQL does not index the referencing side of a foreign key.
 * ProjectController::destroy() deletes silver.campaigns, gold.zone_statistics
 * and gold.element_correlations `WHERE project_id = ?` BEFORE deleting the
 * project (their FKs to silver.projects are NO ACTION, so they block it), and
 * the deferred RI check that follows runs `SELECT 1 ... WHERE project_id = $1`
 * against each of them again. Each table has only (workspace_id, project_id)
 * composites -- a btree that does not LEAD with project_id cannot serve a
 * predicate on it that carries no workspace_id, which the RI check never does
 * (RI queries bypass RLS and add no tenant predicate).
 *
 * targeting.target_recommendations (score_id) is the same shape (FK to
 * targeting.target_scores, ON DELETE RESTRICT) and was deliberately left out of
 * 2026_10_04_200400, which noted it as "outside the list this migration was
 * asked to cover". It is covered here.
 *
 *   silver.campaigns                  (project_id)  FK -> silver.projects         NO ACTION
 *   gold.zone_statistics              (project_id)  FK -> silver.projects         NO ACTION
 *   gold.element_correlations         (project_id)  FK -> silver.projects         NO ACTION
 *   targeting.target_recommendations  (score_id)    FK -> targeting.target_scores RESTRICT
 *
 * ## 2. Byte-identical duplicate workspace_id indexes
 *
 * database/raw/phase0/97 (silver.drill_traces) and 98 (the three gold visual
 * tables) create `idx_<table>_workspace_id` with CREATE INDEX IF NOT EXISTS --
 * which is keyed on the NAME. Four tables already carried the same
 * single-column btree under the name their creating migration gave it
 * (`idx_<table>_workspace`), so on any cluster where db:apply-raw has run each
 * has two identical indexes on (workspace_id): double the write amplification
 * on every insert and update, and double the cache, for no read benefit.
 *
 *   silver.drill_traces                 idx_drill_traces_workspace
 *   gold.drillhole_intervals_visual     idx_drillhole_intervals_visual_workspace
 *   gold.cross_section_panels           idx_cross_section_panels_workspace
 *   gold.structure_measurements_visual  idx_structure_measurements_visual_workspace
 *
 * The older `_workspace` name is dropped, but only after confirming that its
 * `_workspace_id` twin exists, is valid, and has the identical definition
 * (access method, key columns, operator classes, predicate, expressions). The
 * surviving name is the one the raw files use, so every later db:apply-raw
 * finds it and creates nothing. No code references either index name.
 *
 * Limit, stated plainly: on a FRESH cluster `migrate` runs before
 * `db:apply-raw`, so at the moment this migration runs the twin does not exist
 * yet, nothing is dropped, and raw then adds the duplicate. Closing that needs
 * the two raw loops to skip creation when an equivalent index is present; that
 * edit was deliberately not made here because those files run on every deploy
 * and a mistake in them fails the deploy.
 *
 * Tenant isolation is unaffected: plain btree indexes only; no policy, grant or
 * column changes. RLS predicates on workspace_id remain served by the surviving
 * index.
 *
 * CREATE / DROP INDEX CONCURRENTLY, hence $withinTransaction = false (the same
 * pattern as 2026_10_04_200400 / 200700). A table or column absent from a given
 * cluster is skipped. pgsql only.
 */
return new class extends Migration
{
    public $withinTransaction = false;

    /**
     * index name => [qualified table, column].
     *
     * @var array<string, array{0: string, 1: string}>
     */
    private const FK_INDEXES = [
        'idx_campaigns_project_id' => ['silver.campaigns', 'project_id'],
        'idx_zone_statistics_project_id' => ['gold.zone_statistics', 'project_id'],
        'idx_element_correlations_project_id' => ['gold.element_correlations', 'project_id'],
        'idx_target_recommendations_score_id' => ['targeting.target_recommendations', 'score_id'],
    ];

    /**
     * duplicate (older name) => [qualified table, twin that is kept].
     *
     * @var array<string, array{0: string, 1: string}>
     */
    private const DUPLICATES = [
        'idx_drill_traces_workspace' => ['silver.drill_traces', 'idx_drill_traces_workspace_id'],
        'idx_drillhole_intervals_visual_workspace' => ['gold.drillhole_intervals_visual', 'idx_drillhole_intervals_visual_workspace_id'],
        'idx_cross_section_panels_workspace' => ['gold.cross_section_panels', 'idx_cross_section_panels_workspace_id'],
        'idx_structure_measurements_visual_workspace' => ['gold.structure_measurements_visual', 'idx_structure_measurements_visual_workspace_id'],
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (self::FK_INDEXES as $index => [$table, $column]) {
            if (! $this->columnExists($table, $column)) {
                continue;
            }

            $schema = explode('.', $table, 2)[0];
            $this->dropIfInvalid($schema, $index);

            DB::statement("CREATE INDEX CONCURRENTLY IF NOT EXISTS {$index} ON {$table} ({$column})");
        }

        foreach (self::DUPLICATES as $index => [$table, $twin]) {
            if (! $this->identicalValidTwinExists($table, $index, $twin)) {
                continue;
            }

            $schema = explode('.', $table, 2)[0];
            DB::statement("DROP INDEX CONCURRENTLY IF EXISTS {$schema}.{$index}");
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (array_reverse(self::DUPLICATES, true) as $index => [$table]) {
            if (! $this->columnExists($table, 'workspace_id')) {
                continue;
            }

            $schema = explode('.', $table, 2)[0];
            $this->dropIfInvalid($schema, $index);

            DB::statement("CREATE INDEX CONCURRENTLY IF NOT EXISTS {$index} ON {$table} (workspace_id)");
        }

        foreach (array_reverse(self::FK_INDEXES, true) as $index => [$table]) {
            $schema = explode('.', $table, 2)[0];
            DB::statement("DROP INDEX CONCURRENTLY IF EXISTS {$schema}.{$index}");
        }
    }

    private function columnExists(string $qualified, string $column): bool
    {
        return DB::selectOne(
            'SELECT EXISTS (
                SELECT 1 FROM pg_attribute a
                 WHERE a.attrelid = to_regclass(?)
                   AND a.attname = ?
                   AND a.attnum > 0
                   AND NOT a.attisdropped
            ) AS present',
            [$qualified, $column],
        )->present;
    }

    /**
     * True only when $index and $twin are both valid indexes on $table with the
     * same access method, key columns, operator classes, ordering options,
     * predicate and expressions -- i.e. dropping $index loses nothing.
     */
    private function identicalValidTwinExists(string $table, string $index, string $twin): bool
    {
        $schema = explode('.', $table, 2)[0];

        return DB::selectOne(
            'SELECT EXISTS (
                SELECT 1
                  FROM pg_index a
                  JOIN pg_class ca ON ca.oid = a.indexrelid
                  JOIN pg_index b ON b.indrelid = a.indrelid AND b.indexrelid <> a.indexrelid
                  JOIN pg_class cb ON cb.oid = b.indexrelid
                  JOIN pg_namespace n ON n.oid = ca.relnamespace
                 WHERE a.indrelid = to_regclass(?)
                   AND n.nspname = ?
                   AND ca.relname = ?
                   AND cb.relname = ?
                   AND cb.relnamespace = ca.relnamespace
                   AND a.indisvalid AND b.indisvalid
                   AND NOT a.indisunique AND NOT b.indisunique
                   AND NOT a.indisprimary AND NOT b.indisprimary
                   AND ca.relam = cb.relam
                   AND a.indkey::text = b.indkey::text
                   AND a.indclass::text = b.indclass::text
                   AND a.indoption::text = b.indoption::text
                   AND COALESCE(pg_get_expr(a.indpred, a.indrelid), \'\') = COALESCE(pg_get_expr(b.indpred, b.indrelid), \'\')
                   AND COALESCE(pg_get_expr(a.indexprs, a.indrelid), \'\') = COALESCE(pg_get_expr(b.indexprs, b.indrelid), \'\')
            ) AS present',
            [$table, $schema, $index, $twin],
        )->present;
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
