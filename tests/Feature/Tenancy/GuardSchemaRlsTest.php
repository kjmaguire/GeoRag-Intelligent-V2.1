<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Illuminate\Database\QueryException;
use Illuminate\Support\Facades\DB;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\Attributes\Test;
use Tests\TestCase;

/**
 * Plan §2f — workspace-isolation pen-test for the five guard-arm
 * tables added 2026-05-26:
 *
 *   silver.query_traces           (plan §0e)
 *   silver.data_quality_flags     (plan §1g)
 *   silver.document_versions      (plan §1h)
 *   silver.entity_aliases         (plan §1a + §2c)
 *   silver.alias_gaps             (plan §2c)
 *
 * Both tests below run once per table, from the one `guardArmTables()`
 * provider, so a table cannot be in the list and untested (document_versions
 * was: the provider named it, no test method did).
 *
 * Reads. Bind the app.workspace_id GUC to workspace A and insert a row; bind
 * it to workspace B and re-select; B must see ZERO of A's rows, and A must
 * still see its own after the switch.
 *
 * Writes. Bound to workspace B, inserting a row tagged for workspace A must be
 * REJECTED by the policy's check (the policy has a USING clause only, which
 * PostgreSQL also applies to new rows). Isolation that only filters reads
 * would let one tenant plant rows in another's workspace.
 *
 * The canonical RLS pattern on every guard-arm table is:
 *
 *     CREATE POLICY <table>_workspace_isolation ON silver.<table>
 *         USING (workspace_id::text = current_setting('app.workspace_id', true))
 *
 * The test drops to the app role (`georag_app`, no BYPASSRLS) so the policy
 * is enforced. That catches a dropped or loosened policy and a table whose
 * RLS was switched off. It does NOT catch FORCE ROW LEVEL SECURITY being
 * dropped: FORCE only matters to the table OWNER, which this role is not.
 * That flag is pinned at catalog level by WorkspaceRlsCoverageTest.
 *
 * NOTE: this test uses real DB writes inside an outer transaction that
 * always rolls back, so it leaves no residue. RefreshDatabase is NOT used
 * because we explicitly need RLS engaged, which is bypassed by some
 * test-DB roles.
 */
final class GuardSchemaRlsTest extends TestCase
{
    private const WS_A = '11111111-1111-1111-1111-111111111111';

    private const WS_B = '22222222-2222-2222-2222-222222222222';

    /** Placeholder: replaced by a fresh UUID for every insert. */
    private const FRESH_UUID = '{fresh-uuid}';

    /** Placeholder: replaced by the id of a silver.reports row seeded for this test. */
    private const SEEDED_REPORT = '{seeded-report}';

    /**
     * Each row: [table, columns-for-insert-builder].
     * The columns list contains the MINIMUM required NOT NULL fields
     * besides workspace_id; defaults fill the rest.
     *
     * @return array<string, array{0: string, 1: array<string, mixed>}>
     */
    public static function guardArmTables(): array
    {
        return [
            'query_traces' => [
                'silver.query_traces',
                [
                    'query_id' => self::FRESH_UUID,
                    'query_text' => 'rls pen-test',
                ],
            ],
            'data_quality_flags' => [
                'silver.data_quality_flags',
                [
                    'record_type' => 'assay_interval',
                    'record_id' => 'pen-test-record',
                    'flag_type' => 'pen_test_synthetic',
                    'severity' => 'INFO',
                    'description' => 'rls pen-test',
                ],
            ],
            'document_versions' => [
                'silver.document_versions',
                [
                    // FK to silver.reports(report_id): a report row is seeded
                    // for each run (see seedReport()) so the insert is real
                    // instead of being skipped on the FK error.
                    'document_id' => self::SEEDED_REPORT,
                    'report_type' => 'pen_test_report',
                ],
            ],
            'entity_aliases' => [
                'silver.entity_aliases',
                [
                    'entity_type' => 'property',
                    'canonical_name' => 'PenTestProperty',
                    'alias' => 'PTP',
                    'alias_normalised' => self::FRESH_UUID, // unique per insert
                ],
            ],
            'alias_gaps' => [
                'silver.alias_gaps',
                [
                    'entity_text' => 'unknown-pen-test-entity',
                    'entity_text_normalised' => self::FRESH_UUID,
                ],
            ],
        ];
    }

    protected function setUp(): void
    {
        parent::setUp();

        // Skip on sqlite — RLS is a Postgres concept. The default phpunit.xml
        // suite is SQLite; phpunit.pgsql.xml lists this class and runs it.
        if (DB::connection()->getDriverName() !== 'pgsql') {
            $this->markTestSkipped('Workspace isolation pen-test requires PostgreSQL RLS.');
        }

        // From here on we ARE on the Postgres suite, where a missing table or
        // role means the pen-test cannot run. That is a failure, not a skip: a
        // tenant-isolation test that quietly does nothing looks like a pass.
        $missing = DB::selectOne(<<<'SQL'
            SELECT count(*) AS missing
            FROM unnest(ARRAY[
                'query_traces', 'data_quality_flags', 'document_versions', 'entity_aliases', 'alias_gaps'
            ]) AS wanted(name)
            WHERE NOT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'silver' AND table_name = wanted.name
            )
        SQL);
        $this->assertSame(
            0,
            (int) ($missing->missing ?? -1),
            'A guard-arm table is missing on the test DB (migrations 2026_05_26_*). The pen-test cannot run; '.
            'apply them via the pgsql_migrations connection.',
        );

        // Phpunit.pgsql.xml connects as `georag` (owner role, BYPASSRLS=true).
        // To exercise RLS we drop to the app role `georag_app`
        // (BYPASSRLS=false) for the duration of this test. SET ROLE is
        // connection-scoped; tearDown resets it.
        $hasAppRole = DB::selectOne(<<<'SQL'
            SELECT EXISTS (
                SELECT 1 FROM pg_roles
                WHERE rolname = 'georag_app' AND rolbypassrls = false
            ) AS present
        SQL);
        $this->assertTrue(
            (bool) ($hasAppRole->present ?? false),
            'The georag_app role (NOBYPASSRLS) is not provisioned on this PG cluster; the RLS pen-test needs it '.
            'to drop BYPASSRLS. CI creates it before the PHPUnit step.',
        );
        DB::statement('SET ROLE georag_app');
    }

    protected function tearDown(): void
    {
        if (DB::connection()->getDriverName() === 'pgsql') {
            try {
                DB::statement('RESET ROLE');
            } catch (\Throwable $e) {
                // Best-effort cleanup; pgsql may have already closed
                // the connection if the test failed mid-transaction.
            }
        }
        parent::tearDown();
    }

    /**
     * Bind GUC → workspace A, insert row tagged for A, then bind GUC →
     * workspace B and assert SELECT returns zero rows belonging to A.
     * Wrap in a transaction so the synthetic rows roll back.
     *
     * @param array<string, mixed> $columns
     */
    #[Test]
    #[DataProvider('guardArmTables')]
    public function workspace_b_cannot_see_workspace_a_rows(string $table, array $columns): void
    {
        // Pre-flight: make sure the workspaces exist in silver.workspaces.
        // RLS-enabled writes need a referenced workspace row.
        $this->ensureSyntheticWorkspaces();

        DB::beginTransaction();

        try {
            // ── Insert under workspace A ────────────────────────────────
            $this->bindWorkspace(self::WS_A);
            $rowA = array_merge(['workspace_id' => self::WS_A], $this->resolveDeferred($columns, self::WS_A));
            DB::table($table)->insert($rowA);

            $aCount = DB::table($table)
                ->where('workspace_id', self::WS_A)
                ->count();

            $this->assertGreaterThanOrEqual(
                1,
                $aCount,
                "Workspace A should see its own rows in {$table}, got {$aCount}",
            );

            // ── Switch GUC → workspace B, expect 0 of A's rows ──────────
            $this->bindWorkspace(self::WS_B);

            $bSeesA = DB::table($table)
                ->where('workspace_id', self::WS_A)
                ->count();

            $this->assertSame(
                0,
                $bSeesA,
                "RLS LEAK: workspace B saw {$bSeesA} row(s) from workspace A in {$table}",
            );

            // Sanity: switching back to A still sees the row.
            $this->bindWorkspace(self::WS_A);
            $aRecount = DB::table($table)
                ->where('workspace_id', self::WS_A)
                ->count();

            $this->assertSame(
                $aCount,
                $aRecount,
                "Workspace A lost visibility of its own rows after GUC switch in {$table}",
            );
        } finally {
            DB::rollBack();
        }
    }

    /**
     * Bound to workspace B, a row tagged for workspace A must be refused.
     *
     * @param array<string, mixed> $columns
     */
    #[Test]
    #[DataProvider('guardArmTables')]
    public function workspace_b_cannot_write_a_row_tagged_for_workspace_a(string $table, array $columns): void
    {
        $this->ensureSyntheticWorkspaces();

        DB::beginTransaction();

        try {
            $this->bindWorkspace(self::WS_B);
            $rowForA = array_merge(['workspace_id' => self::WS_A], $this->resolveDeferred($columns, self::WS_B));

            try {
                // A nested transaction is a SAVEPOINT: the refused insert
                // aborts only that, and the outer transaction can still roll
                // back cleanly.
                DB::transaction(static fn () => DB::table($table)->insert($rowForA));
            } catch (QueryException $e) {
                $this->assertStringContainsString(
                    'row-level security',
                    $e->getMessage(),
                    "The insert into {$table} was refused, but not by the RLS policy: ".substr($e->getMessage(), 0, 200),
                );

                return;
            }

            $this->fail(
                "RLS WRITE LEAK: bound to workspace B, a row tagged for workspace A was accepted into {$table}",
            );
        } finally {
            DB::rollBack();
        }
    }

    /**
     * Bind the canonical `app.workspace_id` GUC.
     *
     * It used to bind `georag.workspace_id` — the LEGACY name, retired by
     * the 2026-05-28 sweeps — while every policy on these tables reads
     * `app.workspace_id`. The canonical GUC was therefore never set, and
     * because these policies are fail-open on a NULL setting, this test
     * reported a cross-workspace leak on four tables that were in fact
     * configured correctly. Being absent from phpunit.pgsql.xml, it only
     * ever ran under SQLite, where it skipped before reaching any of it.
     * `set_config(..., true)` makes it transaction-local.
     */
    private function bindWorkspace(string $workspaceId): void
    {
        DB::statement(
            "SELECT set_config('app.workspace_id', ?, true)",
            [$workspaceId],
        );
    }

    /**
     * Ensure silver.workspaces has rows for the two synthetic test
     * workspaces. Idempotent — uses ON CONFLICT DO NOTHING.
     *
     * silver.workspaces is owned by the migrations role and georag_app
     * does NOT have INSERT permission (correct for production
     * tenant-isolation). We briefly elevate to `georag` (the owner)
     * for these two synthetic inserts, then drop back to georag_app
     * for the actual RLS pen-test assertions.
     */
    private function ensureSyntheticWorkspaces(): void
    {
        // Temporarily elevate to `georag` (the owner role) for the
        // workspace inserts; drop back to georag_app afterwards so
        // RLS applies to the actual pen-test inserts.
        // Using plain SET ROLE (not SET LOCAL) because this method
        // runs OUTSIDE the test transaction.
        DB::statement('SET ROLE georag');
        try {
            foreach ([self::WS_A, self::WS_B] as $ws) {
                $shortId = substr($ws, 0, 8);
                DB::statement(
                    'INSERT INTO silver.workspaces (workspace_id, name, slug) VALUES (?, ?, ?) '.
                    'ON CONFLICT (workspace_id) DO NOTHING',
                    [$ws, "pen-test-{$shortId}", "pen-test-{$shortId}"],
                );
            }
        } finally {
            DB::statement('SET ROLE georag_app');
        }
    }

    /**
     * Seed a silver.reports row for a document_versions row to point at.
     *
     * Runs INSIDE the test transaction (so it rolls back with it), as the
     * owner role, because the point of the test is the table under test and
     * not who may create reports. Returns to georag_app before it returns.
     */
    private function seedReport(string $workspaceId): string
    {
        $reportId = self::syntheticUuid('r');

        DB::statement('SET ROLE georag');
        try {
            DB::statement(
                'INSERT INTO silver.reports (report_id, title, workspace_id) VALUES (?, ?, ?)',
                [$reportId, 'rls pen-test report', $workspaceId],
            );
        } finally {
            DB::statement('SET ROLE georag_app');
        }

        return $reportId;
    }

    /**
     * Resolve the placeholders in a provider row: a fresh UUID where a unique
     * value is needed, and a seeded report id where a foreign key needs one.
     *
     * @param array<string, mixed> $columns
     *
     * @return array<string, mixed>
     */
    private function resolveDeferred(array $columns, string $reportWorkspaceId): array
    {
        $resolved = [];
        foreach ($columns as $name => $value) {
            $resolved[$name] = match ($value) {
                self::FRESH_UUID => self::syntheticUuid('q'),
                self::SEEDED_REPORT => $this->seedReport($reportWorkspaceId),
                default => $value,
            };
        }

        return $resolved;
    }

    private static function syntheticUuid(string $prefix): string
    {
        // Deterministic-ish but unique-per-call. Good enough for a row id.
        // Map the prefix character to a hex digit so the result is a
        // valid UUID format (PostgreSQL UUID parser rejects 'q', 'g',
        // 'a' as the first nibble). The prefix is preserved as the
        // 13th hex digit (the UUID version nibble) so different
        // prefixes still produce distinct-namespace UUIDs.
        $prefixHexMap = [
            'q' => '0', 'a' => '1', 'g' => '2', 'r' => '3',
            'd' => '4', 'e' => '5', 'f' => '6', 'b' => '7',
            'c' => '8', 'h' => '9',
        ];
        $firstChar = strtolower(substr($prefix, 0, 1));
        $hex = $prefixHexMap[$firstChar] ?? '0';

        return sprintf(
            '%s0000000-%04d-%04d-%04d-%012d',
            $hex,
            random_int(0, 9999),
            random_int(0, 9999),
            random_int(0, 9999),
            random_int(0, 999999999999),
        );
    }
}
