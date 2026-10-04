<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Illuminate\Support\Facades\DB;
use PHPUnit\Framework\Attributes\Test;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * audit.compute_audit_hash() after 2026_10_04_200000: the previous-hash lookup
 * became two indexable branches, and the chain it writes must be exactly the
 * chain the old `IS NOT DISTINCT FROM` lookup wrote.
 *
 * Three claims:
 *  1. chain integrity -- each row's previous_hash is the preceding row's hash
 *     IN ITS OWN CHAIN (a workspace, or the NULL system chain), and each hash is
 *     sha256 of the documented message; computed here independently in PHP and
 *     against audit.recompute_hash(), the SQL mirror verify_hash_chain() uses;
 *  2. the (created_at, id) tiebreak still picks the same parent;
 *  3. both branches are index probes on audit_ledger_workspace_id_idx, not the
 *     sequential scan + sort that cost 57-75 ms per insert at 300k rows.
 *
 * Postgres only (the audit schema and its trigger do not exist on SQLite). All
 * rows are written inside a transaction that is rolled back.
 */
final class AuditChainTriggerLookupTest extends TestCase
{
    use RequiresPostgres;

    private const WS_A = '5a0d1f00-0000-4000-8000-0000000000a1';

    private const WS_B = '5a0d1f00-0000-4000-8000-0000000000b2';

    protected function setUp(): void
    {
        parent::setUp();

        $present = DB::selectOne(
            "SELECT to_regclass('audit.audit_ledger') IS NOT NULL
                AND EXISTS (SELECT 1 FROM pg_trigger
                             WHERE tgrelid = to_regclass('audit.audit_ledger')
                               AND tgname = 'audit_ledger_compute_hash_trg'
                               AND NOT tgisinternal) AS present",
        )->present;

        if (! $present) {
            $this->markTestSkipped('audit.audit_ledger or its hash trigger is absent from this cluster.');
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

    /**
     * @return array{id: string, workspace_id: ?string, previous_hash: ?string, hash: string, created_at: string, payload: string, actor_kind: string, action_type: string}
     */
    private function insertRow(?string $workspace, string $action, string $createdAt, ?string $id = null): array
    {
        $row = DB::selectOne(
            <<<'SQL'
            INSERT INTO audit.audit_ledger (id, workspace_id, actor_kind, action_type, payload, created_at)
            VALUES (COALESCE(?::uuid, gen_random_uuid()), ?::uuid, 'system', ?, '{"k": 1}'::jsonb, ?::timestamptz)
            RETURNING id::text AS id, workspace_id::text AS workspace_id,
                      encode(previous_hash, 'hex') AS previous_hash, encode(hash, 'hex') AS hash,
                      to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS created_at,
                      payload::text AS payload, actor_kind, action_type
            SQL,
            [$id, $workspace, $action, $createdAt],
        );

        return (array) $row;
    }

    /**
     * The documented recipe (docs/audit_ledger_hash_recipe.md), in PHP, with no
     * reliance on the function under test.
     *
     * @param array<string, mixed> $row
     */
    private function expectedHash(?string $previousHex, array $row): string
    {
        return hash('sha256', ($previousHex ?? '')
            .'|'.''           // actor_id
            .'|'.$row['actor_kind']
            .'|'.$row['action_type']
            .'|'.''           // target_schema
            .'|'.''           // target_table
            .'|'.''           // target_id
            .'|'.$row['payload']
            .'|'.$row['created_at']);
    }

    #[Test]
    public function each_chain_links_to_its_own_previous_row_and_hashes_to_the_recipe(): void
    {
        $a1 = $this->insertRow(self::WS_A, 'chain.test', '2031-01-01 00:00:01+00');
        $b1 = $this->insertRow(self::WS_B, 'chain.test', '2031-01-01 00:00:02+00');
        $n1 = $this->insertRow(null, 'chain.test', '2031-01-01 00:00:03+00');
        $a2 = $this->insertRow(self::WS_A, 'chain.test', '2031-01-01 00:00:04+00');
        $n2 = $this->insertRow(null, 'chain.test', '2031-01-01 00:00:05+00');
        $a3 = $this->insertRow(self::WS_A, 'chain.test', '2031-01-01 00:00:06+00');

        // Chain heads have no parent -- the lookups must not leak across chains
        // (A's head does not chain off the NULL chain, nor B's, and vice versa).
        $this->assertNull($a1['previous_hash']);
        $this->assertNull($b1['previous_hash']);
        $this->assertNull($n1['previous_hash']);

        $this->assertSame($a1['hash'], $a2['previous_hash']);
        $this->assertSame($a2['hash'], $a3['previous_hash']);
        $this->assertSame($n1['hash'], $n2['previous_hash'], 'the NULL-workspace (system) chain links through IS NULL');

        foreach ([[$a1, null], [$b1, null], [$n1, null], [$a2, $a1], [$n2, $n1], [$a3, $a2]] as [$row, $parent]) {
            $this->assertSame(
                $this->expectedHash($parent['hash'] ?? null, $row),
                $row['hash'],
                "hash of {$row['id']} departs from the documented recipe",
            );

            $mirror = DB::selectOne(
                "SELECT encode(audit.recompute_hash(
                            decode(?, 'hex'), NULL::bigint, ?, ?, NULL, NULL, NULL, ?::jsonb, ?::timestamptz
                        ), 'hex') AS h",
                [$parent['hash'] ?? '', $row['actor_kind'], $row['action_type'], $row['payload'], $row['created_at']],
            )->h;
            $this->assertSame($mirror, $row['hash'], 'trigger and audit.recompute_hash() disagree');
        }
    }

    #[Test]
    public function rows_sharing_a_created_at_are_ordered_by_id_as_before(): void
    {
        $at = '2031-02-01 00:00:00+00';
        $low = $this->insertRow(self::WS_A, 'tie.test', $at, '00000000-0000-4000-8000-000000000001');
        $high = $this->insertRow(self::WS_A, 'tie.test', $at, '00000000-0000-4000-8000-000000000002');
        $next = $this->insertRow(self::WS_A, 'tie.test', $at, '00000000-0000-4000-8000-000000000003');

        $this->assertSame($low['hash'], $high['previous_hash']);
        $this->assertSame($high['hash'], $next['previous_hash'], 'highest id wins the created_at tie');

        $n1 = $this->insertRow(null, 'tie.test', $at, '00000000-0000-4000-8000-000000000011');
        $n2 = $this->insertRow(null, 'tie.test', $at, '00000000-0000-4000-8000-000000000012');
        $n3 = $this->insertRow(null, 'tie.test', $at, '00000000-0000-4000-8000-000000000013');

        $this->assertSame($n1['hash'], $n2['previous_hash']);
        $this->assertSame($n2['hash'], $n3['previous_hash'], 'same tiebreak on the IS NULL branch');
    }

    #[Test]
    public function both_lookup_branches_are_index_probes_not_sequential_scans(): void
    {
        // The planner prefers a sequential scan on a near-empty table, so take
        // that option away: what is asserted is that an index path EXISTS that
        // serves the filter and the ordering. The old IS NOT DISTINCT FROM form
        // has no such path.
        DB::statement('SET LOCAL enable_seqscan = off');
        DB::statement('SET LOCAL enable_bitmapscan = off');

        $function = DB::selectOne(
            "SELECT pg_get_functiondef('audit.compute_audit_hash()'::regprocedure) AS def",
        )->def;
        $this->assertStringNotContainsString('workspace_id IS NOT DISTINCT FROM NEW.workspace_id)', $function);

        $branches = [
            'workspace' => [
                'SELECT hash FROM audit.audit_ledger WHERE workspace_id = ?::uuid ORDER BY created_at DESC, id DESC LIMIT 1 FOR UPDATE',
                [self::WS_A],
            ],
            'system' => [
                'SELECT hash FROM audit.audit_ledger WHERE workspace_id IS NULL ORDER BY workspace_id, created_at DESC, id DESC LIMIT 1 FOR UPDATE',
                [],
            ],
        ];

        foreach ($branches as $name => [$sql, $bindings]) {
            $plan = collect(DB::select('EXPLAIN (FORMAT JSON) '.$sql, $bindings))
                ->map(fn ($r) => ((array) $r)['QUERY PLAN'])
                ->implode('');

            $this->assertStringContainsString('audit_ledger_workspace_id_idx', $plan, "{$name} branch does not use the index: {$plan}");
            $this->assertStringNotContainsString('"Node Type": "Sort"', $plan, "{$name} branch fully sorts the ledger: {$plan}");
        }

        // The function text carries those same two statements.
        $this->assertStringContainsString('ORDER BY workspace_id, created_at DESC, id DESC', $function);
        $this->assertStringContainsString('WHERE workspace_id = NEW.workspace_id', $function);
    }
}
