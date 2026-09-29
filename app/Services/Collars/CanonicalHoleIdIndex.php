<?php

declare(strict_types=1);

namespace App\Services\Collars;

use Illuminate\Database\ConnectionInterface;
use Illuminate\Database\QueryException;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;

/**
 * The UNIQUE (project_id, hole_id_canonical) index on silver.collars (§04e,
 * SME-approved, Kyle, 2026-09-29) — built only when it can be.
 *
 * Production may already hold ghost collars: two rows of one project whose
 * hole ids canonicalise to the same key (`SRE09-6` / `SRE09_6`). Building a
 * unique index over them fails, and a failed migration fails the deploy. So:
 *
 *   1. backfill `hole_id_canonical` from `silver.canonical_hole_id(hole_id)`
 *      on every row that is NOT part of a duplicate group, plus the oldest
 *      row of each duplicate group whose key no row holds yet — so every
 *      group has exactly one row an upsert keyed on the canonical column can
 *      land on (the one `collars:merge-duplicates` keeps);
 *   2. count duplicate groups; if there are any, log a warning naming the
 *      count and STOP — `php artisan collars:merge-duplicates` merges them,
 *      and the next `php artisan migrate` (or the merge itself) builds the
 *      index;
 *   3. otherwise build `collars_project_id_hole_id_canonical_unique`
 *      CONCURRENTLY (plainly when already inside a transaction, where
 *      CONCURRENTLY is not allowed), then drop the now-redundant partial
 *      `uq_collars_project_hole_canonical` it supersedes.
 *
 * A concurrent build that loses a race (a duplicate inserted mid-build)
 * leaves an INVALID index; it is dropped and reported, never thrown: this
 * runs from a migration and must not fail a deploy.
 *
 * Writers upsert with `ON CONFLICT (project_id, hole_id_canonical) WHERE
 * hole_id_canonical IS NOT NULL`, which infers the partial index before this
 * runs and the full one after, so they work in both states.
 *
 * Stateless: every call takes its connection name, nothing is cached — safe
 * under Octane, though it only ever runs from the CLI.
 */
final class CanonicalHoleIdIndex
{
    public const INDEX = 'collars_project_id_hole_id_canonical_unique';

    public const LEGACY_PARTIAL_INDEX = 'uq_collars_project_hole_canonical';

    public const STATUS_EXISTS = 'exists';

    public const STATUS_CREATED = 'created';

    public const STATUS_SKIPPED_DUPLICATES = 'skipped_duplicates';

    public const STATUS_FAILED = 'failed';

    public const STATUS_UNSUPPORTED = 'unsupported';

    /**
     * Build the index when there are no duplicates; never throws.
     *
     * @return array{status: string, duplicate_groups: int, duplicate_collars: int, backfilled: int, message: string}
     */
    public function ensure(?string $connection = null): array
    {
        $db = DB::connection($connection);

        if ($db->getDriverName() !== 'pgsql' || ! $this->collarsTableExists($db)) {
            return $this->result(self::STATUS_UNSUPPORTED, 0, 0, 0, 'not a Postgres database with silver.collars');
        }

        if ($this->indexIsValid($db)) {
            $this->dropLegacyPartialIndex($db);

            return $this->result(self::STATUS_EXISTS, 0, 0, 0, self::INDEX.' already exists');
        }

        $backfilled = $this->backfillOn($db);
        ['groups' => $groups, 'collars' => $collars] = $this->duplicateCounts($db);

        if ($groups > 0) {
            $message = sprintf(
                'silver.collars has %d duplicate (project_id, canonical hole id) group(s) covering %d collar(s); '
                .'UNIQUE index %s NOT created. Run `php artisan collars:merge-duplicates --dry-run`, then with '
                .'--execute, then re-run `php artisan migrate` (the merge also creates it).',
                $groups, $collars, self::INDEX,
            );
            $this->warn($message, ['duplicate_groups' => $groups, 'duplicate_collars' => $collars]);

            return $this->result(self::STATUS_SKIPPED_DUPLICATES, $groups, $collars, $backfilled, $message);
        }

        $concurrently = $db->transactionLevel() === 0 ? 'CONCURRENTLY ' : '';

        try {
            $this->dropInvalidIndex($db);
            $db->statement(
                'CREATE UNIQUE INDEX '.$concurrently.'IF NOT EXISTS '.self::INDEX
                .' ON silver.collars (project_id, hole_id_canonical)',
            );
        } catch (QueryException $e) {
            if ($db->transactionLevel() === 0) {
                $this->dropInvalidIndex($db);
            }
            $message = 'UNIQUE index '.self::INDEX.' could not be built: '.$e->getMessage();
            $this->warn($message, []);

            return $this->result(self::STATUS_FAILED, 0, 0, $backfilled, $message);
        }

        $this->dropLegacyPartialIndex($db);
        $db->statement(
            'COMMENT ON INDEX silver.'.self::INDEX." IS 'One collar per (project, canonical hole id) - §04e, SME-approved 2026-09-29.'",
        );

        return $this->result(self::STATUS_CREATED, 0, 0, $backfilled, self::INDEX.' created');
    }

    /**
     * Drop the index and restore the partial one it replaced (migration down()).
     */
    public function drop(?string $connection = null): void
    {
        $db = DB::connection($connection);

        if ($db->getDriverName() !== 'pgsql' || ! $this->collarsTableExists($db)) {
            return;
        }

        $concurrently = $db->transactionLevel() === 0 ? 'CONCURRENTLY ' : '';

        // Restored FIRST, so the table is never without a canonical-key
        // arbiter for the writers' ON CONFLICT clause. Cannot fail: the full
        // index being dropped guaranteed uniqueness until now.
        $db->statement(
            'CREATE UNIQUE INDEX '.$concurrently.'IF NOT EXISTS '.self::LEGACY_PARTIAL_INDEX
            .' ON silver.collars (project_id, hole_id_canonical) WHERE hole_id_canonical IS NOT NULL',
        );
        $db->statement('DROP INDEX '.$concurrently.'IF EXISTS silver.'.self::INDEX);
    }

    /**
     * Set `hole_id_canonical` wherever that cannot collide; returns rows set.
     *
     * Rows in a duplicate group keep their old value (a second row taking
     * the key would violate the partial unique index), except that a group
     * where NO row holds the key hands it to its oldest row — the survivor
     * `collars:merge-duplicates` will keep.
     */
    public function backfill(?string $connection = null): int
    {
        return $this->backfillOn(DB::connection($connection));
    }

    /**
     * @return array{groups: int, collars: int}
     */
    public function duplicates(?string $connection = null): array
    {
        return $this->duplicateCounts(DB::connection($connection));
    }

    private function backfillOn(ConnectionInterface $db): int
    {
        return $db->affectingStatement(<<<'SQL'
            WITH ranked AS (
                SELECT collar_id,
                       project_id,
                       silver.canonical_hole_id(hole_id) AS canon,
                       row_number() OVER (
                           PARTITION BY project_id, silver.canonical_hole_id(hole_id)
                           ORDER BY created_at NULLS LAST, collar_id
                       ) AS rn
                  FROM silver.collars
            )
            UPDATE silver.collars c
               SET hole_id_canonical = r.canon
              FROM ranked r
             WHERE c.collar_id = r.collar_id
               AND r.canon IS NOT NULL
               AND r.rn = 1
               AND c.hole_id_canonical IS DISTINCT FROM r.canon
               AND NOT EXISTS (
                   SELECT 1
                     FROM silver.collars o
                    WHERE o.project_id = c.project_id
                      AND o.hole_id_canonical = r.canon
               )
        SQL);
    }

    /**
     * @return array{groups: int, collars: int}
     */
    private function duplicateCounts(ConnectionInterface $db): array
    {
        $row = $db->selectOne(<<<'SQL'
            SELECT count(*)::int AS groups, COALESCE(sum(n), 0)::int AS collars
              FROM (
                  SELECT count(*) AS n
                    FROM silver.collars
                   WHERE silver.canonical_hole_id(hole_id) IS NOT NULL
                   GROUP BY project_id, silver.canonical_hole_id(hole_id)
                  HAVING count(*) > 1
              ) d
        SQL);

        return ['groups' => (int) ($row->groups ?? 0), 'collars' => (int) ($row->collars ?? 0)];
    }

    private function collarsTableExists(ConnectionInterface $db): bool
    {
        $row = $db->selectOne(
            "SELECT to_regclass('silver.collars') IS NOT NULL AS present,
                    to_regprocedure('silver.canonical_hole_id(text)') IS NOT NULL AS fn",
        );

        return (bool) ($row->present ?? false) && (bool) ($row->fn ?? false);
    }

    private function indexIsValid(ConnectionInterface $db): bool
    {
        $row = $db->selectOne(
            'SELECT i.indisvalid AS valid FROM pg_index i WHERE i.indexrelid = to_regclass(?)',
            ['silver.'.self::INDEX],
        );

        return $row !== null && (bool) $row->valid;
    }

    private function dropInvalidIndex(ConnectionInterface $db): void
    {
        $row = $db->selectOne(
            'SELECT i.indisvalid AS valid FROM pg_index i WHERE i.indexrelid = to_regclass(?)',
            ['silver.'.self::INDEX],
        );

        if ($row !== null && ! (bool) $row->valid) {
            $concurrently = $db->transactionLevel() === 0 ? 'CONCURRENTLY ' : '';
            $db->statement('DROP INDEX '.$concurrently.'IF EXISTS silver.'.self::INDEX);
        }
    }

    private function dropLegacyPartialIndex(ConnectionInterface $db): void
    {
        $concurrently = $db->transactionLevel() === 0 ? 'CONCURRENTLY ' : '';
        $db->statement('DROP INDEX '.$concurrently.'IF EXISTS silver.'.self::LEGACY_PARTIAL_INDEX);
    }

    /**
     * @param array<string, int> $context
     */
    private function warn(string $message, array $context): void
    {
        Log::warning($message, $context);

        if (app()->runningInConsole() && ! app()->runningUnitTests()) {
            fwrite(STDERR, '  WARN  '.$message.PHP_EOL);
        }
    }

    /**
     * @return array{status: string, duplicate_groups: int, duplicate_collars: int, backfilled: int, message: string}
     */
    private function result(string $status, int $groups, int $collars, int $backfilled, string $message): array
    {
        return [
            'status' => $status,
            'duplicate_groups' => $groups,
            'duplicate_collars' => $collars,
            'backfilled' => $backfilled,
            'message' => $message,
        ];
    }
}
