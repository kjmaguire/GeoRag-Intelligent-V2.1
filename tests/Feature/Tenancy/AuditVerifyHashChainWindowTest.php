<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Illuminate\Support\Facades\DB;
use PHPUnit\Framework\Attributes\Test;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * audit.verify_hash_chain() after 2026_10_10_100400: the first in-window row of
 * a chain is checked against the newest row of that chain BEFORE the window.
 *
 * It used to take LAG(hash) over the in-window rows only, so a chain's first
 * row in the window had expected_prev = NULL while its stored previous_hash was
 * the (non-NULL) hash of the row before the window, and every workspace with
 * history was reported as a hash-chain break on a perfectly clean ledger
 * (docs/phase0_handoff.md R-P0-8). The nightly `audit_ledger_verify` walk of
 * "the previous 24 h" hit that every night.
 *
 * Postgres only. Every row is written inside a transaction that is rolled
 * back. The hash trigger stamps created_at itself, after the chain lock
 * (2026_10_10_100100), so a test cannot place a row in time: each window edge
 * is read from the database clock between two inserts instead (edge()).
 */
final class AuditVerifyHashChainWindowTest extends TestCase
{
    use RequiresPostgres;

    private const WS_A = '5a0d1f00-0000-4000-8000-0000000c00a1';

    private const WS_B = '5a0d1f00-0000-4000-8000-0000000c00b2';

    protected function setUp(): void
    {
        parent::setUp();

        $present = DB::selectOne(
            "SELECT to_regclass('audit.audit_ledger') IS NOT NULL
                AND to_regprocedure('audit.verify_hash_chain(timestamptz,timestamptz)') IS NOT NULL AS present",
        )->present;

        if (! $present) {
            $this->markTestSkipped('audit.audit_ledger or audit.verify_hash_chain() is absent from this cluster.');
        }

        DB::beginTransaction();
    }

    protected function tearDown(): void
    {
        if (DB::connection()->getDriverName() === 'pgsql' && DB::transactionLevel() > 0) {
            DB::rollBack();
        }

        parent::tearDown();
    }

    private function insertRow(?string $workspace, string $tag): string
    {
        return DB::selectOne(
            "INSERT INTO audit.audit_ledger (workspace_id, actor_kind, action_type, payload)
             VALUES (?::uuid, 'system', 'window.test', jsonb_build_object('tag', ?::text))
             RETURNING id::text AS id",
            [$workspace, $tag],
        )->id;
    }

    /**
     * A point on the ledger's clock that no row shares: pause, read, pause.
     * Rows written before it are before the edge, rows written after are after.
     */
    private function edge(): string
    {
        DB::select('SELECT pg_sleep(0.002)');
        $edge = DB::selectOne('SELECT clock_timestamp()::text AS t')->t;
        DB::select('SELECT pg_sleep(0.002)');

        return $edge;
    }

    /**
     * @return list<string> ids of the rows the verifier reports for the window
     */
    private function breaksBetween(string $start, string $end): array
    {
        return array_map(
            static fn ($r) => $r->audit_id,
            DB::select(
                'SELECT audit_id::text AS audit_id
                   FROM audit.verify_hash_chain(?::timestamptz, ?::timestamptz)
                  ORDER BY created_at',
                [$start, $end],
            ),
        );
    }

    #[Test]
    public function a_clean_ledger_has_no_false_break_at_the_window_edge(): void
    {
        // Chains A and NULL have history before the window; B starts inside it.
        $historyStart = $this->edge();
        $this->insertRow(self::WS_A, 'a1');
        $this->insertRow(self::WS_A, 'a2');
        $this->insertRow(null, 'n1');
        $windowStart = $this->edge();
        $this->insertRow(self::WS_A, 'a3');
        $this->insertRow(null, 'n2');
        $this->insertRow(self::WS_B, 'b1');
        $windowEnd = $this->edge();

        $this->assertSame([], $this->breaksBetween($windowStart, $windowEnd));
        $this->assertSame([], $this->breaksBetween($historyStart, $windowEnd));
    }

    #[Test]
    public function tampering_with_the_pre_window_parent_is_caught_at_the_first_in_window_row(): void
    {
        $this->insertRow(self::WS_A, 'a1');
        $parent = $this->insertRow(self::WS_A, 'a2');
        $windowStart = $this->edge();
        $child = $this->insertRow(self::WS_A, 'a3');
        $this->insertRow(self::WS_A, 'a4');
        $windowEnd = $this->edge();

        DB::statement('SET LOCAL session_replication_role = replica');
        DB::update("UPDATE audit.audit_ledger SET hash = '\\xdeadbeef' WHERE id = ?::uuid", [$parent]);

        $this->assertSame([$child], $this->breaksBetween($windowStart, $windowEnd));
    }

    #[Test]
    public function a_chain_with_no_history_still_expects_a_null_parent(): void
    {
        $windowStart = $this->edge();
        $head = $this->insertRow(self::WS_B, 'b1');
        $windowEnd = $this->edge();

        DB::statement('SET LOCAL session_replication_role = replica');
        DB::update("UPDATE audit.audit_ledger SET previous_hash = '\\xdeadbeef' WHERE id = ?::uuid", [$head]);

        $this->assertSame([$head], $this->breaksBetween($windowStart, $windowEnd));
    }
}
