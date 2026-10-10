<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Illuminate\Support\Facades\DB;
use PHPUnit\Framework\Attributes\Test;
use Tests\Concerns\ProbesAuditLedger;
use Tests\TestCase;

/**
 * 2026_10_10_100100: audit.compute_audit_hash() stamps created_at inside the
 * trigger, after the chain's advisory lock.
 *
 * `created_at` defaults to clock_timestamp(), which is evaluated before a BEFORE
 * INSERT trigger fires and so before the trigger waits for the chain lock. A
 * writer that waited could therefore carry an EARLIER timestamp than the row it
 * links to, and the verifier (which walks each workspace chain in (created_at, id)
 * order) reported a break on a chain nobody touched.
 *
 * The race is reduced here to its effect -- writer B reaches the chain after
 * writer A with a timestamp from before A's -- which needs no concurrency and
 * is deterministic. Every case fails on the previous function body.
 */
final class AuditHashTriggerCreatedAtTest extends TestCase
{
    use ProbesAuditLedger;

    protected function setUp(): void
    {
        parent::setUp();

        if (DB::connection()->getDriverName() !== 'pgsql') {
            $this->markTestSkipped('The audit schema and its triggers are Postgres-only.');
        }

        $this->startAuditProbe();
    }

    protected function tearDown(): void
    {
        $this->endAuditProbe();

        parent::tearDown();
    }

    #[Test]
    public function created_at_is_assigned_by_the_trigger_whatever_the_insert_says(): void
    {
        $before = DB::selectOne('SELECT clock_timestamp() AS t')->t;
        $row = $this->insertRow(self::WS_A, 'stamp.test', '2000-01-01 00:00:00+00');
        $after = DB::selectOne('SELECT clock_timestamp() AS t')->t;

        $stored = DB::selectOne(
            'SELECT created_at >= ?::timestamptz AND created_at <= ?::timestamptz AS inside
               FROM audit.audit_ledger WHERE id = ?::uuid',
            [$before, $after, $row['id']],
        )->inside;

        $this->assertTrue(
            $stored,
            'created_at must be read from the clock inside the trigger, not taken from the INSERT or the column DEFAULT',
        );
    }

    #[Test]
    public function a_writer_with_a_stale_timestamp_cannot_fork_the_chain_order_the_verifier_walks(): void
    {
        // The race, reduced to its effect: writer B reaches the chain AFTER
        // writer A but carries a timestamp from BEFORE A's. With the DEFAULT
        // (or any client value) that is exactly what a writer who waited on the
        // lock looks like.
        $a = $this->insertRow(self::WS_A, 'order.a', '2099-01-01 00:00:00+00');
        $b = $this->insertRow(self::WS_A, 'order.b', '2000-01-01 00:00:00+00');

        $this->assertSame($a['hash'], $b['previous_hash'], 'B links to the chain tail, which is A');

        $bAfterA = DB::selectOne(
            'SELECT (SELECT created_at FROM audit.audit_ledger WHERE id = ?::uuid)
                  > (SELECT created_at FROM audit.audit_ledger WHERE id = ?::uuid) AS ok',
            [$b['id'], $a['id']],
        )->ok;
        $this->assertTrue($bAfterA, 'a child row must not be older than its parent');

        // The verifier orders each workspace chain by (created_at, id). It must
        // agree with the links the trigger stored.
        $breaks = DB::select(
            'SELECT audit_id FROM audit.verify_hash_chain(?::timestamptz, ?::timestamptz) WHERE workspace_id = ?::uuid',
            ['1999-01-01 00:00:00+00', '2100-01-01 00:00:00+00', self::WS_A],
        );
        $this->assertSame([], $breaks, 'verify_hash_chain reports a break on a chain nobody tampered with');
    }

    #[Test]
    public function the_hash_trigger_takes_the_timestamp_after_the_lock_and_holds_no_row_lock(): void
    {
        $def = (string) DB::selectOne(
            "SELECT pg_get_functiondef('audit.compute_audit_hash()'::regprocedure) AS def",
        )->def;
        $code = (string) preg_replace('/--[^\n]*/', '', $def);

        $lock = strpos($code, 'pg_advisory_xact_lock');
        $stamp = strpos($code, 'NEW.created_at := clock_timestamp()');
        $lookup = strpos($code, 'SELECT hash INTO v_prev_hash');

        $this->assertNotFalse($lock);
        $this->assertNotFalse($stamp, 'created_at is not stamped inside the trigger');
        $this->assertNotFalse($lookup);
        $this->assertTrue($lock < $stamp && $stamp < $lookup, 'order must be: lock, stamp, look up the previous row');

        $this->assertDoesNotMatchRegularExpression(
            '/\bFOR\s+(?:NO\s+KEY\s+UPDATE|UPDATE|KEY\s+SHARE|SHARE)\b/i',
            $code,
            'a row lock needs UPDATE on the ledger, which the application role must not hold',
        );
    }
}
