<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Indexes on foreign-key columns that cascade (or restrict) and had none
 * (database audit 2026-10).
 *
 * PostgreSQL does not index the referencing side of a foreign key. When a
 * parent row is deleted -- a project, a workspace, a target model, a score --
 * the ON DELETE CASCADE / RESTRICT check runs `DELETE/SELECT ... WHERE <fk col>
 * = $1` against the child table for every deleted parent, and those RI queries
 * bypass RLS and carry no workspace predicate, so a composite index that merely
 * CONTAINS the column but does not LEAD with it cannot serve them. Deleting a
 * project then costs one sequential scan of each such child table per project
 * (workspace deletion: per workspace's every project), inside one transaction.
 *
 * Each entry below was verified against the live migrated schema: the table and
 * column exist and are the referencing side of an FK to a parent that is
 * deletable.
 *
 *   targeting.target_candidate_zones (project_id)        FK -> silver.projects        CASCADE
 *   targeting.target_candidate_zones (target_model_id)   FK -> targeting.target_models CASCADE
 *   targeting.target_uncertainties   (score_id)          FK -> targeting.target_scores CASCADE
 *   targeting.target_recommendations (zone_id)           FK -> target_candidate_zones  CASCADE
 *   targeting.target_backtests       (model_version_id)  FK -> target_model_versions   CASCADE
 *   silver.geophysics_lines / _line_channels / _dcip_observations / _dcip_models
 *                                    (project_id)        FK -> silver.projects        CASCADE
 *       (each has (workspace_id, project_id), which leads with workspace_id)
 *   silver.las_pending_collar        (workspace_id)      FK -> silver.workspaces      CASCADE
 *   silver.answer_citation_spans     (workspace_id)      FK -> silver.workspaces      CASCADE
 *   silver.collab_comments           (parent_comment_id) FK -> collab_comments        CASCADE
 *
 * Not added, deliberately: targeting.target_recommendations (score_id) is also
 * an unindexed FK (ON DELETE RESTRICT). Same shape, outside the list this
 * migration was asked to cover.
 *
 * Tenant isolation is unaffected: these are plain btree indexes; no policy,
 * grant or column changes.
 *
 * A table or column absent from a given cluster is skipped, not an error (the
 * targeting and geophysics tables are created by migrations a partial restore
 * may not have). CONCURRENTLY, with invalid-leftover cleanup, as in
 * 2026_09_29_210100: $withinTransaction is false.
 *
 * pgsql only.
 */
return new class extends Migration
{
    public $withinTransaction = false;

    /**
     * index name => [qualified table, column].
     *
     * @var array<string, array{0: string, 1: string}>
     */
    private const INDEXES = [
        'idx_target_candidate_zones_project_id' => ['targeting.target_candidate_zones', 'project_id'],
        'idx_target_candidate_zones_target_model_id' => ['targeting.target_candidate_zones', 'target_model_id'],
        'idx_target_uncertainties_score_id' => ['targeting.target_uncertainties', 'score_id'],
        'idx_target_recommendations_zone_id' => ['targeting.target_recommendations', 'zone_id'],
        'idx_target_backtests_model_version_id' => ['targeting.target_backtests', 'model_version_id'],
        'idx_geophysics_lines_project_id' => ['silver.geophysics_lines', 'project_id'],
        'idx_geophysics_line_channels_project_id' => ['silver.geophysics_line_channels', 'project_id'],
        'idx_geophysics_dcip_observations_project_id' => ['silver.geophysics_dcip_observations', 'project_id'],
        'idx_geophysics_dcip_models_project_id' => ['silver.geophysics_dcip_models', 'project_id'],
        'idx_las_pending_collar_workspace_id' => ['silver.las_pending_collar', 'workspace_id'],
        'idx_answer_citation_spans_workspace_id' => ['silver.answer_citation_spans', 'workspace_id'],
        'idx_collab_comments_parent_comment_id' => ['silver.collab_comments', 'parent_comment_id'],
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (self::INDEXES as $index => [$table, $column]) {
            if (! $this->columnExists($table, $column)) {
                continue;
            }

            $schema = explode('.', $table, 2)[0];
            $this->dropIfInvalid($schema, $index);

            DB::statement("CREATE INDEX CONCURRENTLY IF NOT EXISTS {$index} ON {$table} ({$column})");
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
