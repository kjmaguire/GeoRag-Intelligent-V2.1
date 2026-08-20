<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Tests\TestCase;

/**
 * Locks in the contract between `audit.run_verification()` and the table it
 * writes to, after the 2026-08-20 database review found them disagreeing on
 * the Azure server for at least nine consecutive nights.
 *
 * The defect this guards against is not "a migration was missed" — the
 * migration chain was fully applied. It is that the same table has two
 * definitions:
 *
 *   canonical  database/raw/phase0/20-layer-b-audit-ledger.sql   14 columns
 *   mirror     2026_05_14_140000_provision_audit_schema_for_test_db.php  8 columns
 *
 * and both use `CREATE TABLE IF NOT EXISTS`. On a cluster built from the
 * migration chain alone — which is every Azure cluster, because CD runs
 * `laravel-migrate-job` and never applies `database/raw/` — the mirror runs
 * first and wins. The nightly `audit_ledger_verify` workflow then failed on
 * every run with:
 *
 *   UndefinedColumnError: column "workflow_run_id" of relation
 *   "audit_ledger_verification_runs" does not exist
 *
 * meaning the audit-ledger hash chain, a tamper-evidence control, had never
 * actually been verified in production.
 *
 * This test runs against the test DB, which is provisioned by the migration
 * chain — the same path Azure takes. That is precisely why it catches the bug:
 * it exercises the environment where the mirror wins, not the developer
 * cluster where `database/raw/` has been applied by hand and everything looks
 * fine. Repaired by 2026_08_20_030000_restore_canonical_audit_verification_schema.
 *
 * Skipped on SQLite — the audit schema is Postgres-only.
 */
final class AuditVerificationSchemaContractTest extends TestCase
{
    use RefreshDatabase;

    private const TABLE_SCHEMA = 'audit';

    private const TABLE_NAME = 'audit_ledger_verification_runs';

    /**
     * Every column `audit.run_verification()` names, taken straight from the
     * function body in database/raw/phase0/100-audit-verify-function.sql.
     *
     *   INSERT ... (id, partition_date, status, started_at, workflow_run_id)
     *   UPDATE ... SET status, rows_verified, first_id, last_id,
     *                  first_hash, last_hash, broken_ids, completed_at
     *
     * If the function grows a column, add it here and to the migration in the
     * same change — that is the whole point of pinning it.
     *
     * @var list<string>
     */
    private const REQUIRED_COLUMNS = [
        'id',
        'partition_date',
        'status',
        'started_at',
        'completed_at',
        'workflow_run_id',
        'rows_verified',
        'first_id',
        'last_id',
        'first_hash',
        'last_hash',
        'broken_ids',
    ];

    /**
     * The function's first write is `status = 'in_progress'`. The mirror's
     * CHECK allowed only ('pending','clean','break','error'), so restoring the
     * columns alone would have swapped an UndefinedColumn error for a
     * CheckViolation on the very same INSERT. Both vocabularies must pass.
     *
     * @var list<string>
     */
    private const REQUIRED_STATUS_VALUES = ['in_progress', 'clean', 'break', 'error'];

    protected function setUp(): void
    {
        parent::setUp();

        if (DB::connection()->getDriverName() !== 'pgsql') {
            $this->markTestSkipped('audit schema is Postgres-only.');
        }
    }

    public function test_verification_runs_table_has_every_column_the_function_writes(): void
    {
        $present = $this->columns();

        $this->assertNotEmpty(
            $present,
            sprintf(
                '%s.%s does not exist. The audit schema is provisioned earlier in the chain.',
                self::TABLE_SCHEMA,
                self::TABLE_NAME,
            ),
        );

        $missing = array_values(array_diff(self::REQUIRED_COLUMNS, $present));

        $this->assertSame(
            [],
            $missing,
            sprintf(
                "audit.run_verification() writes column(s) that %s.%s does not have: %s\n".
                "This is the 2026-08-20 regression: a *_for_test_db mirror migration defined a\n".
                "narrower table than database/raw/phase0/20-layer-b-audit-ledger.sql, and won\n".
                'because CREATE TABLE IF NOT EXISTS ran first. The nightly hash-chain verify '.
                'fails silently when this happens.',
                self::TABLE_SCHEMA,
                self::TABLE_NAME,
                implode(', ', $missing),
            ),
        );
    }

    public function test_status_check_accepts_the_value_the_function_writes_first(): void
    {
        $definition = DB::selectOne(
            "SELECT pg_get_constraintdef(c.oid) AS def
               FROM pg_constraint c
               JOIN pg_class t ON t.oid = c.conrelid
               JOIN pg_namespace n ON n.oid = t.relnamespace
              WHERE n.nspname = ?
                AND t.relname = ?
                AND c.contype = 'c'
                AND pg_get_constraintdef(c.oid) LIKE '%status%'",
            [self::TABLE_SCHEMA, self::TABLE_NAME],
        );

        $this->assertNotNull(
            $definition,
            'Expected a CHECK constraint on status; found none.',
        );

        foreach (self::REQUIRED_STATUS_VALUES as $value) {
            $this->assertStringContainsString(
                "'".$value."'",
                $definition->def,
                sprintf(
                    "The status CHECK rejects '%s', which audit.run_verification() writes.\n".
                    'Constraint is: %s',
                    $value,
                    $definition->def,
                ),
            );
        }
    }

    public function test_the_verification_function_chain_is_installed(): void
    {
        foreach (['recompute_hash', 'verify_hash_chain', 'run_verification'] as $function) {
            $this->assertNotNull(
                DB::selectOne(
                    'SELECT 1 AS present
                       FROM pg_proc p
                       JOIN pg_namespace n ON n.oid = p.pronamespace
                      WHERE n.nspname = ? AND p.proname = ?',
                    [self::TABLE_SCHEMA, $function],
                ),
                sprintf(
                    "audit.%s() is not installed.\n".
                    'It is declared in database/raw/phase0/100-audit-verify-function.sql, which CD '.
                    'never applies, and mirrored into 2026_08_20_030000 so the migration chain '.
                    'carries it. If this fails, that migration was reverted or the raw file drifted.',
                    $function,
                ),
            );
        }
    }

    /**
     * @return list<string>
     */
    private function columns(): array
    {
        $rows = DB::select(
            'SELECT column_name
               FROM information_schema.columns
              WHERE table_schema = ? AND table_name = ?',
            [self::TABLE_SCHEMA, self::TABLE_NAME],
        );

        return array_map(static fn (object $r): string => $r->column_name, $rows);
    }
}
