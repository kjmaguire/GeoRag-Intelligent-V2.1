<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Illuminate\Support\Facades\DB;
use PHPUnit\Framework\Attributes\Test;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * audit.verify_hash_chain() after 2026_10_10_100000: the first in-window row of
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
 * back, with timestamps far in the future so nothing real shares the window.
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

    private function insertRow(?string $workspace, string $tag, string $createdAt): string
    {
        return DB::selectOne(
            "INSERT INTO audit.audit_ledger (workspace_id, actor_kind, action_type, payload, created_at)
             VALUES (?::uuid, 'system', 'window.test', jsonb_build_object('tag', ?::text), ?::timestamptz)
             RETURNING id::text AS id",
            [$workspace, $tag, $createdAt],
        )->id;
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
        // Chains A and NULL have history on day 1; B starts on day 2.
        $this->insertRow(self::WS_A, 'a1', '2032-01-01 00:00:01+00');
        $this->insertRow(self::WS_A, 'a2', '2032-01-01 00:00:02+00');
        $this->insertRow(null, 'n1', '2032-01-01 00:00:03+00');
        $this->insertRow(self::WS_A, 'a3', '2032-01-02 00:00:01+00');
        $this->insertRow(null, 'n2', '2032-01-02 00:00:02+00');
        $this->insertRow(self::WS_B, 'b1', '2032-01-02 00:00:03+00');

        $this->assertSame([], $this->breaksBetween('2032-01-02 00:00:00+00', '2032-01-03 00:00:00+00'));
        $this->assertSame([], $this->breaksBetween('2032-01-01 00:00:00+00', '2032-01-03 00:00:00+00'));
    }

    #[Test]
    public function tampering_with_the_pre_window_parent_is_caught_at_the_first_in_window_row(): void
    {
        $this->insertRow(self::WS_A, 'a1', '2032-02-01 00:00:01+00');
        $parent = $this->insertRow(self::WS_A, 'a2', '2032-02-01 00:00:02+00');
        $child = $this->insertRow(self::WS_A, 'a3', '2032-02-02 00:00:01+00');
        $this->insertRow(self::WS_A, 'a4', '2032-02-02 00:00:02+00');

        DB::statement('SET LOCAL session_replication_role = replica');
        DB::update("UPDATE audit.audit_ledger SET hash = '\\xdeadbeef' WHERE id = ?::uuid", [$parent]);

        $this->assertSame([$child], $this->breaksBetween('2032-02-02 00:00:00+00', '2032-02-03 00:00:00+00'));
    }

    #[Test]
    public function a_chain_with_no_history_still_expects_a_null_parent(): void
    {
        $head = $this->insertRow(self::WS_B, 'b1', '2032-03-02 00:00:01+00');

        DB::statement('SET LOCAL session_replication_role = replica');
        DB::update("UPDATE audit.audit_ledger SET previous_hash = '\\xdeadbeef' WHERE id = ?::uuid", [$head]);

        $this->assertSame([$head], $this->breaksBetween('2032-03-02 00:00:00+00', '2032-03-03 00:00:00+00'));
    }
}
