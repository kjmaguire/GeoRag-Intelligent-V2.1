<?php

declare(strict_types=1);

namespace App\Services\Collars;

use Illuminate\Database\Connection;
use Illuminate\Support\Facades\DB;
use RuntimeException;
use Throwable;

/**
 * Merge ghost collars — rows of one project whose hole ids canonicalise to
 * the same key — into the OLDEST row of each group (§04e, SME-approved, Kyle,
 * 2026-09-29), so `UNIQUE (project_id, hole_id_canonical)` can be built.
 *
 * Per group, in order:
 *
 *   1. `app.workspace_id` is bound (transaction-local) to the group's
 *      workspace, so every statement below runs under the same RLS scope the
 *      application would — including the fail-closed tables
 *      (silver.sample_intervals, silver.las_pending_collar). A group whose
 *      collars disagree on workspace_id is refused, not merged.
 *   2. The survivor keeps every value it has; a NULL elevation / total_depth
 *      / azimuth / dip / drill_date / drill_type / hole_status is filled from
 *      the newest ghost that has one. Nothing it recorded is overwritten.
 *   3. Every table with a foreign key to silver.collars — discovered from
 *      pg_constraint, not hard-coded, so a table added next month is covered
 *      — has its ghost rows re-pointed to the survivor. Where a unique index
 *      on that table includes the collar column (a trace per collar, a curve
 *      name per collar, a gold summary per collar) and the survivor already
 *      holds the same key, the GHOST's row is dropped and counted: the
 *      survivor's own data wins, and the dry run shows how many.
 *   4. bronze.provenance (target_table = 'collars') and
 *      silver.las_pending_collar carry collar ids without a foreign key; they
 *      are re-pointed too, so lineage follows the merge.
 *   5. The ghosts are deleted, the survivor takes the canonical key, and the
 *      project's data_version is bumped so tile caches and the Workspace
 *      refetch.
 *
 * Everything runs in ONE transaction. `dryRun` performs exactly the same
 * statements and rolls back, so its counts are the real ones, conflicts
 * included; any error rolls the whole merge back.
 *
 * Not done here, on purpose: gold tables and silver.drill_traces are derived
 * and are rebuilt by the next promote_silver_to_gold run (the nightly sweep,
 * or an ingest); silver.mv_collar_summary by the next MV refresh.
 * audit.audit_ledger rows naming a ghost are an append-only record and are
 * left as written.
 */
final class CollarDuplicateMerger
{
    /** Columns a ghost may fill on the survivor when the survivor has NULL. */
    private const FILLABLE = [
        'elevation', 'total_depth', 'azimuth', 'dip', 'drill_date', 'drill_type', 'hole_status',
    ];

    public function __construct(private readonly CanonicalHoleIdIndex $index) {}

    /**
     * The duplicate groups, oldest collar first in each.
     *
     * @return list<array{project_id: string, canonical: string, workspace_ids: list<string>, collars: list<array{collar_id: string, hole_id: string, created_at: ?string}>}>
     */
    public function groups(?string $connection = null, ?string $projectId = null): array
    {
        $db = DB::connection($connection);
        $bindings = [];
        $projectFilter = '';
        if ($projectId !== null) {
            $projectFilter = 'AND project_id = ?::uuid';
            $bindings[] = $projectId;
        }

        $rows = $db->select(<<<SQL
            SELECT project_id::text AS project_id,
                   silver.canonical_hole_id(hole_id) AS canonical,
                   array_to_json(array_agg(DISTINCT workspace_id::text)) AS workspace_ids,
                   json_agg(
                       json_build_object(
                           'collar_id', collar_id::text,
                           'hole_id', hole_id,
                           'created_at', created_at::text
                       )
                       ORDER BY created_at NULLS LAST, collar_id
                   ) AS collars
              FROM silver.collars
             WHERE silver.canonical_hole_id(hole_id) IS NOT NULL
                   {$projectFilter}
             GROUP BY project_id, silver.canonical_hole_id(hole_id)
            HAVING count(*) > 1
             ORDER BY project_id, canonical
        SQL, $bindings);

        $groups = [];
        foreach ($rows as $row) {
            /** @var list<string> $workspaceIds */
            $workspaceIds = array_values(array_filter(
                (array) json_decode((string) $row->workspace_ids, true),
                static fn (mixed $v): bool => is_string($v) && $v !== '',
            ));
            /** @var list<array{collar_id: string, hole_id: string, created_at: ?string}> $collars */
            $collars = json_decode((string) $row->collars, true);
            $groups[] = [
                'project_id' => (string) $row->project_id,
                'canonical' => (string) $row->canonical,
                'workspace_ids' => $workspaceIds,
                'collars' => $collars,
            ];
        }

        return $groups;
    }

    /**
     * Merge every group (or only one project's); roll back when $dryRun.
     *
     * @return array{dry_run: bool, groups: list<array<string, mixed>>, refused: list<array<string, mixed>>, totals: array<string, int>, index: array<string, mixed>|null}
     */
    public function merge(?string $connection = null, bool $dryRun = true, ?string $projectId = null): array
    {
        $db = DB::connection($connection);
        if ($db->getDriverName() !== 'pgsql') {
            throw new RuntimeException('collars:merge-duplicates needs the Postgres connection.');
        }

        $groups = $this->groups($connection, $projectId);
        $children = $this->childTables($db);

        $report = ['dry_run' => $dryRun, 'groups' => [], 'refused' => [], 'totals' => [
            'groups' => 0, 'ghosts_deleted' => 0, 'rows_repointed' => 0, 'rows_dropped_as_duplicates' => 0,
        ], 'index' => null];

        $db->beginTransaction();
        try {
            foreach ($groups as $group) {
                if (count($group['workspace_ids']) !== 1) {
                    $report['refused'][] = $group + ['reason' => 'collars of this group belong to different workspaces'];

                    continue;
                }
                $result = $this->mergeGroup($db, $group, $children);
                $report['groups'][] = $result;
                $report['totals']['groups']++;
                $report['totals']['ghosts_deleted'] += count($result['ghosts']);
                foreach ($result['tables'] as $counts) {
                    $report['totals']['rows_repointed'] += $counts['repointed'];
                    $report['totals']['rows_dropped_as_duplicates'] += $counts['dropped'];
                }
            }

            if ($dryRun) {
                $db->rollBack();
            } else {
                $db->commit();
            }
        } catch (Throwable $e) {
            $db->rollBack();

            throw $e;
        }

        if (! $dryRun) {
            $report['index'] = $this->index->ensure($connection);
        }

        return $report;
    }

    /**
     * @param array{project_id: string, canonical: string, workspace_ids: list<string>, collars: list<array{collar_id: string, hole_id: string, created_at: ?string}>} $group
     * @param list<array{table: string, column: string, uniques: list<list<string>>}> $children
     *
     * @return array{project_id: string, canonical: string, survivor: array{collar_id: string, hole_id: string, created_at: ?string}, ghosts: list<array{collar_id: string, hole_id: string, created_at: ?string}>, filled: list<string>, tables: array<string, array{repointed: int, dropped: int}>}
     */
    private function mergeGroup(Connection $db, array $group, array $children): array
    {
        $db->select("SELECT set_config('app.workspace_id', ?, true)", [$group['workspace_ids'][0]]);

        $survivor = $group['collars'][0];
        $ghosts = array_slice($group['collars'], 1);
        $survivorId = $survivor['collar_id'];

        $filled = $this->fillSurvivor($db, $survivorId, array_reverse($ghosts));

        $tables = [];
        foreach ($ghosts as $ghost) {
            foreach ($children as $child) {
                $counts = $this->repoint($db, $child, $ghost['collar_id'], $survivorId);
                $key = $child['table'].'.'.$child['column'];
                $tables[$key] = [
                    'repointed' => ($tables[$key]['repointed'] ?? 0) + $counts['repointed'],
                    'dropped' => ($tables[$key]['dropped'] ?? 0) + $counts['dropped'],
                ];
            }

            $provenance = $db->affectingStatement(
                "UPDATE bronze.provenance SET target_id = ?::uuid
                  WHERE target_schema = 'silver' AND target_table = 'collars' AND target_id = ?::uuid",
                [$survivorId, $ghost['collar_id']],
            );
            $tables['bronze.provenance.target_id'] = [
                'repointed' => ($tables['bronze.provenance.target_id']['repointed'] ?? 0) + $provenance,
                'dropped' => 0,
            ];

            if ($this->relationExists($db, 'silver.las_pending_collar')) {
                $pending = $db->affectingStatement(
                    'UPDATE silver.las_pending_collar SET collar_id = ?::uuid, updated_at = now() WHERE collar_id = ?::uuid',
                    [$survivorId, $ghost['collar_id']],
                );
                $tables['silver.las_pending_collar.collar_id'] = [
                    'repointed' => ($tables['silver.las_pending_collar.collar_id']['repointed'] ?? 0) + $pending,
                    'dropped' => 0,
                ];
            }
        }

        $ghostIds = array_column($ghosts, 'collar_id');
        $deleted = $db->affectingStatement(
            'DELETE FROM silver.collars WHERE collar_id = ANY(?::uuid[])',
            [$this->pgArray($ghostIds)],
        );
        if ($deleted !== count($ghostIds)) {
            throw new RuntimeException(sprintf(
                'Expected to delete %d ghost collar(s) of %s / %s, deleted %d; nothing was changed.',
                count($ghostIds), $group['project_id'], $group['canonical'], $deleted,
            ));
        }

        // The survivor may have carried a stale key (the raw hole id a .log
        // writer stored) while a ghost held the real one; with the ghosts
        // gone the trigger can hand it the canonical value.
        $db->update(
            'UPDATE silver.collars SET hole_id_canonical = silver.canonical_hole_id(hole_id), updated_at = now()
              WHERE collar_id = ?::uuid AND hole_id_canonical IS DISTINCT FROM silver.canonical_hole_id(hole_id)',
            [$survivorId],
        );
        $db->update(
            'UPDATE silver.projects SET data_version = data_version + 1 WHERE project_id = ?::uuid',
            [$group['project_id']],
        );

        ksort($tables);

        return [
            'project_id' => $group['project_id'],
            'canonical' => $group['canonical'],
            'survivor' => $survivor,
            'ghosts' => $ghosts,
            'filled' => $filled,
            'tables' => array_filter($tables, static fn (array $c): bool => $c['repointed'] > 0 || $c['dropped'] > 0),
        ];
    }

    /**
     * Fill the survivor's NULL attributes from the ghosts, newest first.
     *
     * @param list<array{collar_id: string, hole_id: string, created_at: ?string}> $ghostsNewestFirst
     *
     * @return list<string> the columns that were filled
     */
    private function fillSurvivor(Connection $db, string $survivorId, array $ghostsNewestFirst): array
    {
        if ($ghostsNewestFirst === []) {
            return [];
        }

        $columns = implode(', ', self::FILLABLE);
        $survivor = (array) $db->selectOne(
            "SELECT {$columns} FROM silver.collars WHERE collar_id = ?::uuid",
            [$survivorId],
        );

        $set = [];
        $bindings = [];
        foreach ($ghostsNewestFirst as $ghost) {
            $row = (array) $db->selectOne(
                "SELECT {$columns} FROM silver.collars WHERE collar_id = ?::uuid",
                [$ghost['collar_id']],
            );
            foreach (self::FILLABLE as $column) {
                if (($survivor[$column] ?? null) === null && ($row[$column] ?? null) !== null && ! isset($set[$column])) {
                    $set[$column] = "{$column} = ?";
                    $bindings[] = $row[$column];
                }
            }
        }

        if ($set === []) {
            return [];
        }

        $bindings[] = $survivorId;
        $db->update(
            'UPDATE silver.collars SET '.implode(', ', $set).', updated_at = now() WHERE collar_id = ?::uuid',
            $bindings,
        );

        return array_keys($set);
    }

    /**
     * @param array{table: string, column: string, uniques: list<list<string>>} $child
     *
     * @return array{repointed: int, dropped: int}
     */
    private function repoint(Connection $db, array $child, string $ghostId, string $survivorId): array
    {
        $table = $child['table'];
        $column = $this->quoteIdent($child['column']);
        $dropped = 0;

        foreach ($child['uniques'] as $others) {
            $match = array_map(
                fn (string $c): string => 's.'.$this->quoteIdent($c).' = g.'.$this->quoteIdent($c),
                $others,
            );
            $dropped += $db->affectingStatement(
                "DELETE FROM {$table} g
                  WHERE g.{$column} = ?::uuid
                    AND EXISTS (
                        SELECT 1 FROM {$table} s
                         WHERE s.{$column} = ?::uuid".($match === [] ? '' : ' AND '.implode(' AND ', $match)).'
                    )',
                [$ghostId, $survivorId],
            );
        }

        $repointed = $db->affectingStatement(
            "UPDATE {$table} SET {$column} = ?::uuid WHERE {$column} = ?::uuid",
            [$survivorId, $ghostId],
        );

        return ['repointed' => $repointed, 'dropped' => $dropped];
    }

    /**
     * Every single-column foreign key to silver.collars, with the other
     * columns of each plain unique index that includes the key column.
     *
     * @return list<array{table: string, column: string, uniques: list<list<string>>}>
     */
    private function childTables(Connection $db): array
    {
        $fks = $db->select(<<<'SQL'
            SELECT format('%I.%I', n.nspname, cl.relname) AS tbl,
                   c.conrelid::oid AS relid,
                   a.attname AS col,
                   a.attnum AS attnum
              FROM pg_constraint c
              JOIN pg_class cl ON cl.oid = c.conrelid
              JOIN pg_namespace n ON n.oid = cl.relnamespace
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
             WHERE c.contype = 'f'
               AND c.confrelid = 'silver.collars'::regclass
               AND cardinality(c.conkey) = 1
             ORDER BY 1, 3
        SQL);

        $out = [];
        foreach ($fks as $fk) {
            $indexes = $db->select(<<<'SQL'
                SELECT i.indexrelid,
                       array_to_json(array_agg(a.attname::text ORDER BY k.ord)) AS cols,
                       bool_or(a.attnum = ?) AS has_fk
                  FROM pg_index i
                 CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
                  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
                 WHERE i.indrelid = ?::oid
                   AND i.indisunique
                   AND i.indpred IS NULL
                   AND NOT (0 = ANY (i.indkey::int2[]))
                 GROUP BY i.indexrelid
            SQL, [(int) $fk->attnum, (int) $fk->relid]);

            $uniques = [];
            foreach ($indexes as $index) {
                if (! (bool) $index->has_fk) {
                    continue;
                }
                /** @var list<string> $cols */
                $cols = json_decode((string) $index->cols, true);
                $uniques[] = array_values(array_filter($cols, static fn (string $c): bool => $c !== $fk->col));
            }

            $out[] = ['table' => (string) $fk->tbl, 'column' => (string) $fk->col, 'uniques' => $uniques];
        }

        return $out;
    }

    private function relationExists(Connection $db, string $relation): bool
    {
        return (bool) ($db->selectOne('SELECT to_regclass(?) IS NOT NULL AS present', [$relation])->present ?? false);
    }

    private function quoteIdent(string $identifier): string
    {
        return '"'.str_replace('"', '""', $identifier).'"';
    }

    /**
     * @param list<string> $values
     */
    private function pgArray(array $values): string
    {
        return '{'.implode(',', $values).'}';
    }
}
