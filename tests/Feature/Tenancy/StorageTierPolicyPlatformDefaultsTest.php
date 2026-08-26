<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Illuminate\Support\Facades\DB;
use PHPUnit\Framework\Attributes\Test;
use Tests\TestCase;

/**
 * Pins the two silver.storage_tier_policy fixes from
 * 2026_08_25_200000_restore_platform_default_visibility_on_storage_tier_policy.
 *
 * 1. workspace_id NULL means PLATFORM DEFAULT (the table's own comment and
 *    the Storage Tiering Agent's `workspace_id IS NULL OR workspace_id = $1`
 *    query both say so), and the policy must make those rows visible to a
 *    workspace-bound session — while staying fail-closed for tenant rows:
 *    an unbound GUC sees only the defaults, never another tenant's overrides.
 *    Before the fix, the strict fail-closed shape hid every platform default
 *    from the application (0 rows with the GUC bound, measured on live Azure
 *    2026-08-25) and the agent ran against an empty rule set.
 *
 * 2. The unique constraint must be NULLS NOT DISTINCT, or the seed's
 *    ON CONFLICT DO NOTHING never fires for NULL-workspace rows and every
 *    re-apply of the re-runnable phase0 raw SQL duplicates the defaults
 *    (dev had reached 90 copies of each rule).
 *
 * Gated like FailClosedRlsPolicyTest: Postgres only, georag_app must exist
 * without BYPASSRLS, and the table must exist (it is created only by the
 * phase0 raw SQL, so the migrate-only test database skips — see
 * scripts/raw-parity-baseline.txt).
 */
final class StorageTierPolicyPlatformDefaultsTest extends TestCase
{
    private const QUALIFIED = 'silver.storage_tier_policy';

    private const POLICY = 'silver_storage_tier_policy_workspace_isolation';

    private const WORKSPACE_A = '4f1c1f3e-0000-4000-8000-00000000001a';

    private const WORKSPACE_B = '4f1c1f3e-0000-4000-8000-00000000001b';

    /** Distinctive object_class so probes never collide with real rules. */
    private const PROBE_CLASS = 'zz_probe_platform_defaults';

    protected function setUp(): void
    {
        parent::setUp();

        if (DB::connection()->getDriverName() !== 'pgsql') {
            $this->markTestSkipped('RLS is Postgres-only.');
        }

        if (DB::selectOne('SELECT to_regclass(?) IS NOT NULL AS present', [self::QUALIFIED])?->present !== true) {
            $this->markTestSkipped(self::QUALIFIED.' is absent from this cluster (phase0 raw SQL not applied).');
        }

        $hasAppRole = DB::selectOne(<<<'SQL'
            SELECT EXISTS (
                SELECT 1 FROM pg_roles
                 WHERE rolname = 'georag_app' AND rolbypassrls = false
            ) AS present
        SQL);

        if (! ($hasAppRole->present ?? false)) {
            $this->markTestSkipped(
                'georag_app role not provisioned on this PG cluster — '.
                'the visibility probe needs a role without BYPASSRLS.',
            );
        }
    }

    protected function tearDown(): void
    {
        if (DB::connection()->getDriverName() === 'pgsql') {
            try {
                DB::statement('RESET ROLE');
            } catch (\Throwable) {
                // Connection may already have closed mid-test.
            }
        }

        parent::tearDown();
    }

    /**
     * Catalog half: exactly one policy, its USING carries the platform-default
     * exemption on the COLUMN, and neither fail-open unbound-GUC branch
     * (which tests the GUC itself for NULL) has crept back in.
     */
    #[Test]
    public function policy_exempts_null_workspace_rows_without_being_fail_open(): void
    {
        $rows = DB::select(
            <<<'SQL'
            SELECT pol.polname, pg_get_expr(pol.polqual, pol.polrelid) AS qual
              FROM pg_policy pol
             WHERE pol.polrelid = to_regclass(?)
            SQL,
            [self::QUALIFIED],
        );

        $this->assertCount(
            1,
            $rows,
            self::QUALIFIED.' must carry exactly one policy — permissive policies OR '
            .'together, so a leftover second policy can silently reopen the table. Found: '
            .implode(', ', array_map(static fn ($r) => $r->polname, $rows)),
        );

        $this->assertSame(self::POLICY, $rows[0]->polname);

        $qual = (string) $rows[0]->qual;

        $this->assertStringContainsString(
            'workspace_id IS NULL',
            $qual,
            'The platform-default exemption is gone — NULL-workspace rows are invisible '
            .'to the Storage Tiering Agent again: '.$qual,
        );

        foreach ([
            "NULLIF(current_setting('app.workspace_id'::text, true), ''::text) IS NULL",
            "current_setting('app.workspace_id'::text, true) IS NULL",
        ] as $failOpenBranch) {
            $this->assertStringNotContainsString(
                $failOpenBranch,
                $qual,
                'Policy '.self::POLICY.' has a fail-open unbound-GUC branch: '.$qual,
            );
        }
    }

    /**
     * Write half: pin polcmd and polwithcheck, which the catalog test above
     * does not read.
     *
     * The policy is created as `CREATE POLICY ... USING (...)` with no FOR and
     * no WITH CHECK, so Postgres makes it FOR ALL and reuses USING as the
     * WITH CHECK. That is deliberate — the migration docblock explains that a
     * session with write grants has to be able to maintain the platform rows,
     * because the seed INSERT and this migration's own dedupe DELETE run under
     * FORCE ROW LEVEL SECURITY and would otherwise be unable to touch them.
     *
     * It has a cost worth stating out loud. georag_app holds
     * `GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA ... silver ...`
     * (database/raw/phase1/10-georag-app-role.sql:49-51) and is NOBYPASSRLS,
     * and the pre-#188 macro's WITH CHECK *did* reject NULL-workspace writes
     * from a workspace-bound session. So the write surface on the NULL scope
     * is genuinely wider now: a bound session that could execute arbitrary SQL
     * could INSERT or UPDATE a platform-default row, which every tenant reads.
     * No application code writes this table today — the tiering agent only
     * SELECTs from it — so this is a defence-in-depth gap, not a live hole.
     *
     * This test does not object to that trade-off. It stops it from changing
     * by accident: if anyone adds a writer to this table, or narrows the
     * policy, this fails and forces the decision to be made deliberately.
     */
    #[Test]
    public function policy_write_surface_stays_as_deliberately_chosen(): void
    {
        $rows = DB::select(
            <<<'SQL'
            SELECT pol.polcmd::text                                  AS cmd,
                   pg_get_expr(pol.polwithcheck, pol.polrelid)       AS with_check
              FROM pg_policy pol
             WHERE pol.polrelid = to_regclass(?)
            SQL,
            [self::QUALIFIED],
        );

        $this->assertCount(1, $rows);

        $this->assertSame(
            '*',
            $rows[0]->cmd,
            'Policy '.self::POLICY.' is no longer FOR ALL. If it was split into '
            .'separate read/write policies, that is very likely an improvement — '
            .'update this test deliberately rather than deleting it.',
        );

        $this->assertNull(
            $rows[0]->with_check,
            'Policy '.self::POLICY.' now has an explicit WITH CHECK. That changes '
            .'who may write platform-default (NULL workspace_id) rows, which the '
            .'seed and the dedupe depend on being able to do under FORCE RLS. '
            .'Confirm both still work before updating this expectation. Got: '
            .var_export($rows[0]->with_check, true),
        );
    }

    /**
     * Behavioural half: one platform row and two tenant overrides, read back
     * as georag_app under four GUC states. Runs in a rolled-back transaction
     * so the probe rows never outlive the test.
     */
    #[Test]
    public function platform_defaults_are_visible_to_every_binding_but_tenant_rows_stay_isolated(): void
    {
        DB::beginTransaction();

        try {
            DB::insert(
                <<<'SQL'
                INSERT INTO silver.workspaces (workspace_id, name, slug)
                VALUES (?::uuid, 'RLS probe A', 'zz-rls-probe-a'),
                       (?::uuid, 'RLS probe B', 'zz-rls-probe-b')
                SQL,
                [self::WORKSPACE_A, self::WORKSPACE_B],
            );

            DB::insert(
                <<<'SQL'
                INSERT INTO silver.storage_tier_policy
                    (workspace_id, object_class, source_tier, target_tier, age_threshold_days)
                VALUES (NULL,   ?, 'hot',  'warm', 7),
                       (?::uuid, ?, 'hot',  'cold', 7),
                       (?::uuid, ?, 'warm', 'cold', 7)
                SQL,
                [
                    self::PROBE_CLASS,
                    self::WORKSPACE_A, self::PROBE_CLASS,
                    self::WORKSPACE_B, self::PROBE_CLASS,
                ],
            );

            $this->assertSame(3, $this->probeCount(), 'owner should see all three probe rows');

            DB::statement('SET LOCAL ROLE georag_app');

            // 1. GUC never set: platform default only — fail-closed for tenants.
            $this->assertSame(
                1,
                $this->probeCount(),
                'Unbound GUC must see exactly the platform default — more means tenant rows leak, '
                .'fewer means the NULL exemption is gone.',
            );

            // 2. Empty-string sentinel (BindWorkspaceRlsContext binds '' whenever
            //    it cannot resolve a workspace, and again in its finally block).
            DB::statement("SELECT set_config('app.workspace_id', '', true)");
            $this->assertSame(1, $this->probeCount(), "GUC = '' must behave exactly like unbound.");

            // 3. Bound to A: platform default + A's override, and NOT B's.
            DB::statement("SELECT set_config('app.workspace_id', ?, true)", [self::WORKSPACE_A]);
            $this->assertSame(
                2,
                $this->probeCount(),
                'Workspace A must see the platform default plus its own override — this is the '
                .'agent query shape (workspace_id IS NULL OR workspace_id = $1) that returned '
                .'zero rows before the fix.',
            );
            $this->assertSame(
                0,
                $this->probeCountFor(self::WORKSPACE_B),
                "Workspace A can see workspace B's override — the exemption over-opened the table.",
            );

            // 4. Bound to B: same, from the other side.
            DB::statement("SELECT set_config('app.workspace_id', ?, true)", [self::WORKSPACE_B]);
            $this->assertSame(2, $this->probeCount());
        } finally {
            DB::rollBack();
        }
    }

    /**
     * The duplication half: the constraint is NULLS NOT DISTINCT, so the
     * phase0 seed's ON CONFLICT DO NOTHING now actually fires for
     * NULL-workspace rows instead of inserting a copy per re-apply — and no
     * duplicates survived the migration's dedupe.
     */
    #[Test]
    public function null_workspace_rows_conflict_instead_of_duplicating(): void
    {
        $def = (string) DB::selectOne(
            <<<'SQL'
            SELECT pg_get_constraintdef(oid) AS def
              FROM pg_constraint
             WHERE conrelid = to_regclass(?)
               AND conname = 'storage_tier_policy_unique_per_scope'
            SQL,
            [self::QUALIFIED],
        )?->def;

        $this->assertStringContainsString('NULLS NOT DISTINCT', $def);

        $leftoverDuplicates = DB::select(
            <<<'SQL'
            SELECT object_class, source_tier, target_tier, count(*) AS n
              FROM silver.storage_tier_policy
             WHERE workspace_id IS NULL
             GROUP BY 1, 2, 3
            HAVING count(*) > 1
            SQL,
        );
        $this->assertSame([], $leftoverDuplicates, 'NULL-workspace rules are still duplicated.');

        DB::beginTransaction();

        try {
            $seedShapedInsert = <<<'SQL'
                INSERT INTO silver.storage_tier_policy
                    (workspace_id, object_class, source_tier, target_tier, age_threshold_days)
                VALUES (NULL, ?, 'hot', 'warm', 7)
                ON CONFLICT (workspace_id, object_class, source_tier, target_tier) DO NOTHING
                SQL;

            $this->assertSame(1, DB::affectingStatement($seedShapedInsert, [self::PROBE_CLASS]));
            $this->assertSame(
                0,
                DB::affectingStatement($seedShapedInsert, [self::PROBE_CLASS]),
                'A seed-shaped re-insert of a NULL-workspace rule wrote a duplicate — '
                .'ON CONFLICT is not firing, so raw-SQL re-applies will multiply the defaults again.',
            );
        } finally {
            DB::rollBack();
        }
    }

    private function probeCount(): int
    {
        return (int) DB::selectOne(
            'SELECT count(*) AS n FROM silver.storage_tier_policy WHERE object_class = ?',
            [self::PROBE_CLASS],
        )->n;
    }

    private function probeCountFor(string $workspaceId): int
    {
        return (int) DB::selectOne(
            'SELECT count(*) AS n FROM silver.storage_tier_policy WHERE object_class = ? AND workspace_id = ?::uuid',
            [self::PROBE_CLASS, $workspaceId],
        )->n;
    }
}
