<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Index for external_notification's duplicate-delivery check
 * (database audit 2026-10).
 *
 * hatchet_workflows/external_notification.py `_already_recorded` runs, on every
 * inbound notification,
 *
 *     SELECT id::text FROM audit.audit_ledger
 *      WHERE action_type = 'external_notification.received'
 *        AND payload->>'notification_id' = $1
 *      ORDER BY created_at DESC LIMIT 1
 *
 * The only index that mentions action_type is (action_type, created_at DESC),
 * which finds every `external_notification.received` row in the partition set
 * and then filters the JSON out of each -- a scan of all notifications ever
 * received, growing with every one.
 *
 * Partial on that one action_type and keyed on the JSON field, so it holds only
 * notification rows (a tiny fraction of the ledger) and costs nothing on every
 * other audit insert, which matters because the hash-chain trigger already
 * serialises those. `created_at DESC` is the trailing column so the ORDER BY ...
 * LIMIT 1 is satisfied by the index order. The predicate is a literal the query
 * also uses, so the planner proves it implies the index's WHERE.
 *
 * audit.audit_ledger is range-partitioned in production, and CREATE INDEX
 * CONCURRENTLY is not supported on a partitioned parent. A plain CREATE INDEX on
 * the parent would take SHARE on EVERY partition for the whole build; with the
 * 10 s lock_timeout 2026_10_04_200600 puts on georag_app, audit inserts would
 * start failing for the duration on a large ledger. So on a partitioned ledger
 * this uses the documented three-step form instead:
 *
 *   1. CREATE INDEX ... ON ONLY audit.audit_ledger  -- no data scanned, only the
 *      parent catalog entry; the index is INVALID until every partition's index
 *      is attached. Takes SHARE on the parent only, briefly, under a 30 s
 *      lock_timeout so it fails and retries rather than queueing behind a writer.
 *   2. CREATE INDEX CONCURRENTLY IF NOT EXISTS on each partition (found through
 *      pg_partition_tree, so sub-partitioned trees and the default partition are
 *      covered). Writers are not blocked.
 *   3. ALTER INDEX ... ATTACH PARTITION for each; the parent becomes valid on the
 *      last one. Sub-partitioned intermediate nodes get ON ONLY and are attached
 *      bottom-up.
 *
 * Re-runnable: a partition that already has an index attached to the parent index
 * is skipped, an INVALID leftover from a failed CONCURRENTLY build is dropped and
 * rebuilt, and the parent is IF NOT EXISTS. Hence $withinTransaction = false
 * (CONCURRENTLY cannot run in a transaction block). A partition created AFTER
 * this runs inherits the index automatically from the parent.
 *
 * A non-partitioned audit.audit_ledger (a cluster without partitions) gets a single
 * CREATE INDEX CONCURRENTLY IF NOT EXISTS.
 *
 * pgsql only. down() drops the parent index, which cascades to the attached
 * partition indexes, then any unattached leftover.
 */
return new class extends Migration
{
    public $withinTransaction = false;

    private const INDEX = 'audit_ledger_external_notification_id_idx';

    private const DEFINITION = "((payload->>'notification_id'), created_at DESC) WHERE action_type = 'external_notification.received'";

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        $relkind = DB::selectOne("SELECT relkind FROM pg_class WHERE oid = to_regclass('audit.audit_ledger')")->relkind ?? null;
        if ($relkind === null) {
            return;
        }

        if ($relkind !== 'p') {
            $this->dropIfInvalid('audit', self::INDEX);
            DB::statement('CREATE INDEX CONCURRENTLY IF NOT EXISTS '.self::INDEX.' ON audit.audit_ledger '.self::DEFINITION);

            return;
        }

        $this->createOnPartitionedLedger();
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP INDEX IF EXISTS audit.'.self::INDEX);

        // Anything a failed or interrupted up() left unattached on a partition.
        foreach ($this->partitions() as $partition) {
            $name = $this->quote($partition->nspname).'.'.$this->quote($this->partitionIndexName($partition->relname));
            // A partitioned (sub-partition) index cannot be dropped CONCURRENTLY.
            DB::statement($partition->relkind === 'p' ? "DROP INDEX IF EXISTS {$name}" : "DROP INDEX CONCURRENTLY IF EXISTS {$name}");
        }
    }

    private function createOnPartitionedLedger(): void
    {
        $parent = 'audit.'.self::INDEX;

        $this->withLockTimeout(function (): void {
            DB::statement('CREATE INDEX IF NOT EXISTS '.self::INDEX.' ON ONLY audit.audit_ledger '.self::DEFINITION);
        });

        $partitions = $this->partitions();

        // Build leaves, and ON ONLY for intermediate nodes, parents before children
        // (the query orders by level ascending).
        foreach ($partitions as $partition) {
            $qualified = $this->quote($partition->nspname).'.'.$this->quote($partition->relname);
            $name = $this->partitionIndexName($partition->relname);
            $indexQualified = $this->quote($partition->nspname).'.'.$this->quote($name);

            if ($this->hasAttachedIndex($partition->relname, $partition->nspname, $partition->parent_relname, $partition->parent_nspname)) {
                continue;
            }

            if ($partition->relkind === 'p') {
                DB::statement("CREATE INDEX IF NOT EXISTS {$this->quote($name)} ON ONLY {$qualified} ".self::DEFINITION);
            } else {
                $this->dropIfInvalid($partition->nspname, $name);
                DB::statement("CREATE INDEX CONCURRENTLY IF NOT EXISTS {$this->quote($name)} ON {$qualified} ".self::DEFINITION);
            }

            $parentIndex = $partition->parent_relname === 'audit_ledger' && $partition->parent_nspname === 'audit'
                ? $parent
                : $this->quote($partition->parent_nspname).'.'.$this->quote($this->partitionIndexName($partition->parent_relname));

            $this->withLockTimeout(function () use ($parentIndex, $indexQualified): void {
                DB::statement("ALTER INDEX {$parentIndex} ATTACH PARTITION {$indexQualified}");
            });
        }
    }

    /**
     * Every partition below audit.audit_ledger, shallowest first, with its direct
     * parent. Ordered so an ON ONLY intermediate index exists before its children
     * attach to it.
     *
     * @return list<object{relname: string, nspname: string, relkind: string, parent_relname: string, parent_nspname: string}>
     */
    private function partitions(): array
    {
        $relkind = DB::selectOne("SELECT relkind FROM pg_class WHERE oid = to_regclass('audit.audit_ledger')")->relkind ?? null;
        if ($relkind !== 'p') {
            return [];
        }

        return DB::select(
            "SELECT c.relname, n.nspname, c.relkind, pc.relname AS parent_relname, pn.nspname AS parent_nspname
               FROM pg_partition_tree('audit.audit_ledger'::regclass) t
               JOIN pg_class c ON c.oid = t.relid
               JOIN pg_namespace n ON n.oid = c.relnamespace
               JOIN pg_class pc ON pc.oid = t.parentrelid
               JOIN pg_namespace pn ON pn.oid = pc.relnamespace
              WHERE t.level > 0 AND c.relkind IN ('r', 'p')
              ORDER BY t.level, c.relname",
        );
    }

    /**
     * Whether the partition already carries an index attached to the parent's
     * external-notification index (a re-run, or a half-finished earlier run).
     */
    private function hasAttachedIndex(string $relname, string $nspname, string $parentRelname, string $parentNspname): bool
    {
        $parentIndexName = $parentRelname === 'audit_ledger' && $parentNspname === 'audit'
            ? self::INDEX
            : $this->partitionIndexName($parentRelname);

        return DB::selectOne(
            'SELECT EXISTS (
                SELECT 1
                  FROM pg_inherits i
                  JOIN pg_class ic ON ic.oid = i.inhrelid
                  JOIN pg_index x ON x.indexrelid = ic.oid
                  JOIN pg_class pi ON pi.oid = i.inhparent
                  JOIN pg_namespace pn ON pn.oid = pi.relnamespace
                 WHERE x.indrelid = ?::regclass
                   AND pi.relname = ?
                   AND pn.nspname = ?
            ) AS attached',
            [$this->quote($nspname).'.'.$this->quote($relname), $parentIndexName, $parentNspname],
        )->attached;
    }

    /**
     * Postgres truncates identifiers at 63 bytes; truncate the partition name
     * ourselves so the name is stable between up(), the re-run check and down().
     */
    private function partitionIndexName(string $relname): string
    {
        $suffix = '_extnotif_idx';

        return substr($relname, 0, 63 - strlen($suffix)).$suffix;
    }

    private function quote(string $identifier): string
    {
        return '"'.str_replace('"', '""', $identifier).'"';
    }

    /**
     * Run a short catalog-only statement under a lock_timeout that is restored
     * afterwards (this migration is not in a transaction, so SET LOCAL would not
     * hold).
     */
    private function withLockTimeout(callable $statement): void
    {
        DB::statement("SET lock_timeout = '30s'");
        try {
            $statement();
        } finally {
            DB::statement('RESET lock_timeout');
        }
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
            DB::statement('DROP INDEX CONCURRENTLY IF EXISTS '.$this->quote($schema).'.'.$this->quote($index));
        }
    }
};
