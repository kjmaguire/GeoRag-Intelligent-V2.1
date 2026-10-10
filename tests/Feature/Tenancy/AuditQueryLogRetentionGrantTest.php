<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Illuminate\Support\Facades\DB;
use PHPUnit\Framework\Attributes\Test;
use Tests\Concerns\ProbesAuditLedger;
use Tests\TestCase;

/**
 * 2026_10_10_100000: `georag_app` may DELETE from audit.query_audit_log.
 *
 * retention_sweep (src/fastapi/app/hatchet_workflows/retention_sweep.py) purges
 * that table, then silver.ingest_progress. On AWS the worker is `georag_app`,
 * which held INSERT/SELECT/UPDATE on the whole `audit` schema and no DELETE, so
 * the first statement raised `permission denied for table query_audit_log` and
 * the ingest_progress purge after it never ran.
 *
 * The table is a plain RAG query log, NOT the hash-chained ledger; the last two
 * cases pin both facts. Fails on the previous state of the database (no DELETE
 * grant). The same purge SQL, run for real as georag_app against a
 * migrate-then-db:apply-raw database, is in src/fastapi/tests/test_rls_after_raw.py.
 */
final class AuditQueryLogRetentionGrantTest extends TestCase
{
    use ProbesAuditLedger;

    protected function setUp(): void
    {
        parent::setUp();

        if (DB::connection()->getDriverName() !== 'pgsql') {
            $this->markTestSkipped('The audit schema and its roles are Postgres-only.');
        }

        $this->startAuditProbe();
    }

    protected function tearDown(): void
    {
        $this->endAuditProbe();

        parent::tearDown();
    }

    #[Test]
    public function the_app_role_can_delete_from_query_audit_log(): void
    {
        $this->assertTrue($this->hasPrivilege('georag_app', 'audit.query_audit_log', 'DELETE'));

        DB::insert(
            "INSERT INTO audit.query_audit_log (query_id, query_text, created_at)
             VALUES ('ledger-test-old', 'q', now() - interval '400 days')",
        );
        $this->asAppRole();

        // Same shape as retention_sweep._QUERY_AUDIT_BATCH_SQL.
        $deleted = DB::delete(
            "DELETE FROM audit.query_audit_log
              WHERE audit_id IN (
                    SELECT audit_id FROM audit.query_audit_log
                     WHERE query_id = 'ledger-test-old' AND created_at < now() - (180 * interval '1 day')
                     LIMIT 5000)",
        );
        $this->assertSame(1, $deleted);
    }

    #[Test]
    public function query_audit_log_is_not_part_of_the_hash_chain(): void
    {
        $columns = array_map(
            static fn ($r) => $r->column_name,
            DB::select(
                "SELECT column_name FROM information_schema.columns
                  WHERE table_schema = 'audit' AND table_name = 'query_audit_log'",
            ),
        );
        $this->assertNotContains('hash', $columns);
        $this->assertNotContains('previous_hash', $columns);

        $this->assertSame(
            0,
            (int) DB::selectOne(
                "SELECT count(*) AS n FROM pg_trigger
                  WHERE tgrelid = 'audit.query_audit_log'::regclass AND NOT tgisinternal",
            )->n,
            'a trigger on query_audit_log would mean it participates in a chain',
        );
    }

    #[Test]
    public function the_delete_grant_is_reversible(): void
    {
        $migration = $this->migration('grant_delete');

        $migration->down();
        $this->assertFalse($this->hasPrivilege('georag_app', 'audit.query_audit_log', 'DELETE'));

        $migration->up();
        $this->assertTrue($this->hasPrivilege('georag_app', 'audit.query_audit_log', 'DELETE'));
    }

    #[Test]
    public function the_ledger_itself_never_gets_the_delete_grant(): void
    {
        $this->migration('grant_delete')->up();

        $this->assertFalse(
            $this->hasPrivilege('georag_app', 'audit.audit_ledger', 'DELETE'),
            'the retention grant is for query_audit_log only',
        );
    }
}
