<?php

declare(strict_types=1);

namespace Tests\Feature\Database;

use Illuminate\Database\QueryException;
use Illuminate\Support\Facades\DB;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\Attributes\Test;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Second-pass database audit follow-ups, read back from the catalogs:
 *
 *   200300  external-notification index exists, is valid, and on a partitioned
 *           ledger is attached on every partition (the ON ONLY + CONCURRENTLY +
 *           ATTACH PARTITION build)
 *   200500  the SECURITY DEFINER hardening survives a migrating role that does
 *           not own the functions (no abort, NOTICE only)
 *   200700  the bare (collar_id) indexes superseded by the (collar_id,
 *           source_file) ones from 100000 are gone, the composites are valid
 *
 * Tenant isolation is not touched (indexes and function ACLs only; no policy,
 * column or RLS change). Postgres only (run with -c phpunit.pgsql.xml).
 */
final class AuditIndexAndSecurityDefinerMigrationsTest extends TestCase
{
    use RequiresPostgres;

    /**
     * @return array<string, array{0: string, 1: string, 2: string}>
     */
    public static function supersededIndexes(): array
    {
        return [
            'structure' => ['silver.structure', 'idx_structure_collar_id', 'idx_structure_collar_source_file'],
            'alteration' => ['silver.alteration', 'idx_alteration_collar_id', 'idx_alteration_collar_source_file'],
            'mineralization' => ['silver.mineralization', 'idx_mineralization_collar_id', 'idx_mineralization_collar_source_file'],
        ];
    }

    #[Test]
    #[DataProvider('supersededIndexes')]
    public function bare_collar_id_index_is_dropped_and_the_composite_that_covers_it_is_valid(string $table, string $bare, string $composite): void
    {
        if (! DB::selectOne('SELECT to_regclass(?) IS NOT NULL AS present', [$table])->present) {
            $this->markTestSkipped("{$table} is absent from this cluster.");
        }

        $this->assertNull($this->indexRow($table, $bare), "{$bare} is redundant and should be dropped");

        $row = $this->indexRow($table, $composite);
        $this->assertNotNull($row, "{$composite} is missing: the only collar_id index is gone");
        $this->assertTrue($row->valid, "{$composite} is INVALID");
        $this->assertStringContainsString('(collar_id, source_file)', $row->def);
    }

    #[Test]
    public function external_notification_index_is_valid_and_attached_on_every_partition(): void
    {
        $relkind = DB::selectOne("SELECT relkind FROM pg_class WHERE oid = to_regclass('audit.audit_ledger')")->relkind ?? null;
        if ($relkind === null) {
            $this->markTestSkipped('audit.audit_ledger is absent from this cluster.');
        }

        $parent = $this->indexRow('audit.audit_ledger', 'audit_ledger_external_notification_id_idx');
        $this->assertNotNull($parent);
        $this->assertTrue($parent->valid, 'parent index is INVALID: a partition has no attached index');
        $this->assertStringContainsString("WHERE (action_type = 'external_notification.received'::text)", $parent->def);

        if ($relkind !== 'p') {
            return;
        }

        $unindexed = DB::select(
            "SELECT t.relid::regclass::text AS partition
               FROM pg_partition_tree('audit.audit_ledger'::regclass) t
              WHERE t.isleaf
                AND NOT EXISTS (
                    SELECT 1
                      FROM pg_inherits i
                      JOIN pg_index x ON x.indexrelid = i.inhrelid
                     WHERE i.inhparent = 'audit.audit_ledger_external_notification_id_idx'::regclass
                       AND x.indrelid = t.relid AND x.indisvalid
                )",
        );
        $this->assertSame([], array_column($unindexed, 'partition'));
    }

    /**
     * 200500's ALTER FUNCTION needs ownership. The migration must log and move
     * on for a role that lacks it, not abort the migration run.
     */
    #[Test]
    public function definer_hardening_does_not_abort_for_a_migrating_role_that_does_not_own_the_functions(): void
    {
        $me = DB::selectOne('SELECT rolsuper, rolcreaterole FROM pg_roles WHERE rolname = current_user');
        if (! ($me->rolsuper || $me->rolcreaterole)
            || ! DB::selectOne("SELECT to_regprocedure('workflow.get_flow_jwt_keys(text)') IS NOT NULL AS present")->present) {
            $this->markTestSkipped('Needs a role that can CREATE ROLE, and the workflow functions.');
        }

        DB::beginTransaction();
        try {
            DB::statement('CREATE ROLE migrate_nonowner_probe NOLOGIN');
            DB::statement('GRANT USAGE ON SCHEMA workflow, usage TO migrate_nonowner_probe');
            DB::statement('SET LOCAL ROLE migrate_nonowner_probe');

            // Precondition: unguarded, this is exactly the failure being fixed.
            try {
                DB::transaction(static function (): void {
                    DB::statement('ALTER FUNCTION workflow.get_flow_jwt_keys(text) SET search_path = pg_catalog');
                });
                $this->fail('a non-owner ALTER FUNCTION was expected to be refused');
            } catch (QueryException $e) {
                $this->assertSame('42501', $e->getCode());
            }

            $migration = require base_path('database/migrations/2026_10_04_200500_harden_security_definer_functions.php');
            $migration->up();
            $migration->down();

            DB::statement('RESET ROLE');

            // Nothing was changed on the functions the role does not own.
            $config = DB::selectOne("SELECT proconfig FROM pg_proc WHERE oid = 'workflow.get_flow_jwt_keys(text)'::regprocedure")->proconfig;
            $this->assertStringContainsString('search_path=pg_catalog, workflow, public', (string) $config);
            $this->assertStringNotContainsString('search_path=workflow, public, pg_catalog', (string) $config);
        } finally {
            DB::rollBack();
        }
    }

    private function indexRow(string $table, string $index): ?object
    {
        return DB::selectOne(
            'SELECT i.indisvalid AS valid, pg_get_indexdef(c.oid) AS def
               FROM pg_index i
               JOIN pg_class c ON c.oid = i.indexrelid
              WHERE c.relname = ? AND i.indrelid = to_regclass(?)',
            [$index, $table],
        );
    }
}
