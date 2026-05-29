<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Tests\TestCase;

/**
 * Locks in the tenancy invariant exposed by the Lakehouse audit
 * 2026-05-25: any application table that carries a workspace_id column
 * MUST have RLS enabled with at least one policy.
 *
 * This is the durable backstop for the recurring pattern (CC-01/CC-03
 * + reliability spec migrations all forgot ENABLE ROW LEVEL SECURITY).
 * If a future migration adds a workspace_id column without the matching
 * policy, this test wakes up.
 *
 * Skipped on SQLite — RLS is a Postgres feature.
 *
 * Excluded schemas:
 *   - public                — Laravel's own tables (users, jobs, etc.)
 *                             carry no workspace_id and are user-scoped.
 *   - laravel-managed       — sessions, password_resets, etc.
 *
 * Excluded tables fall into three categories:
 *   1. Self-referential (silver.workspaces — tenant policy would block
 *      the membership lookup it depends on).
 *   2. Partition children — PostgreSQL doesn't propagate policies down
 *      the partition tree, but queries that go through the parent (the
 *      normal access path) get pruned + policy-evaluated correctly.
 *      Filtered via pg_inherits.
 *   3. Test-DB parity casualties — production picks up RLS from
 *      database/raw/phase0/96-rls-tenant-* SQL files that aren't run by
 *      RefreshDatabase. Listed in EXEMPT_TEST_DB_ONLY_TABLES below
 *      with verification dates so the list ages out as the test-DB
 *      bootstrap improves.
 */
final class WorkspaceRlsCoverageTest extends TestCase
{
    use RefreshDatabase;

    /**
     * Permanent exemptions (verified safe in every environment).
     *
     * @var list<string>
     */
    private const EXEMPT_TABLES = [
        // The workspaces registry itself — RLS would block reading the
        // very rows used to evaluate workspace membership.
        'silver.workspaces',
    ];

    /**
     * Reserved for future test-DB-only exemptions. Currently empty —
     * the 14 tables previously listed here were reconciled into a
     * proper Laravel migration on 2026-05-25
     * (2026_05_25_175214_enable_rls_on_phase0_workspace_tables_reconciliation),
     * which is a no-op against production (existing policies left
     * untouched) and a first-time install against the test DB.
     *
     * Keep the constant in place so future test-DB-parity gaps have
     * an obvious home; future entries MUST include a follow-up note
     * for how they'll be reconciled.
     *
     * @var list<string>
     */
    private const EXEMPT_TEST_DB_ONLY_TABLES = [];

    public function test_every_workspace_scoped_table_has_rls_with_a_policy(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('RLS is Postgres-only.');
        }

        // Exclude partition children — pg_inherits.inhrelid means the
        // table is a child of a partitioned table, and partition policies
        // live on the parent (Postgres prunes + evaluates from there).
        $rows = DB::select(<<<'SQL'
            SELECT n.nspname AS schema,
                   c.relname AS table,
                   c.relrowsecurity AS rls_on,
                   EXISTS (
                     SELECT 1 FROM pg_policies p
                     WHERE p.schemaname = n.nspname AND p.tablename = c.relname
                   ) AS has_policy
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname IN ('silver','gold','bronze','audit','public_geo','index')
              AND c.relkind = 'r'
              AND NOT EXISTS (
                SELECT 1 FROM pg_inherits i WHERE i.inhrelid = c.oid
              )
              AND EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = n.nspname
                  AND table_name = c.relname
                  AND column_name = 'workspace_id'
              )
            ORDER BY 1, 2
        SQL);

        $allExempt = array_merge(self::EXEMPT_TABLES, self::EXEMPT_TEST_DB_ONLY_TABLES);
        $gaps = [];
        foreach ($rows as $r) {
            $qualified = $r->schema.'.'.$r->table;
            if (in_array($qualified, $allExempt, true)) {
                continue;
            }
            if (! $r->rls_on || ! $r->has_policy) {
                $gaps[] = sprintf(
                    '%s (rls_on=%s, has_policy=%s)',
                    $qualified,
                    $r->rls_on ? 'true' : 'false',
                    $r->has_policy ? 'true' : 'false',
                );
            }
        }

        $this->assertSame(
            [],
            $gaps,
            'Tables with workspace_id but no RLS+policy: '.PHP_EOL.implode(PHP_EOL, $gaps).
            PHP_EOL.PHP_EOL.
            'Fix by adding ENABLE ROW LEVEL SECURITY + a workspace_isolation policy '.
            'in a new migration. See 2026_05_25_173814_enable_rls_on_post_phase0_workspace_tables '.
            'for the canonical template, or add to EXEMPT_TABLES with a comment if exempt.',
        );
    }

    /**
     * SECURITY regression test for the 2026-05-25 broken-GUC sweep.
     *
     * The original WorkspaceRlsCoverageTest above checks that RLS is
     * enabled + a policy exists, but a policy that references the wrong
     * GUC name still counts as "present" — yet behaves fail-open
     * because the GUC is never set by any app codepath. We caught 12
     * such policies during the deferred-items pass (silver.document_
     * passages, silver.answer_runs, etc.) — all using `georag.workspace_id`
     * or `georag.project_id` instead of the canonical `app.workspace_id`.
     * Migration 2026_05_25_180924 replaced them with the canonical
     * shape; this test stops the pattern from regressing.
     */
    public function test_no_policy_references_legacy_georag_gucs(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('RLS is Postgres-only.');
        }

        $offenders = DB::select(<<<'SQL'
            SELECT schemaname, tablename, policyname
              FROM pg_policies
             WHERE qual LIKE '%georag.workspace_id%'
                OR qual LIKE '%georag.project_id%'
                OR with_check LIKE '%georag.workspace_id%'
                OR with_check LIKE '%georag.project_id%'
             ORDER BY schemaname, tablename, policyname
        SQL);

        $msg = 'Policies still reference the legacy `georag.*` GUC namespace, '
            .'which is functionally fail-open because the app sets `app.workspace_id`. '
            .'Replace with the canonical workspace_isolation shape — see '
            .'2026_05_25_180924_replace_broken_guc_rls_policies_with_canonical '
            .'for the template.';

        $list = array_map(
            fn ($r) => "  {$r->schemaname}.{$r->tablename} → {$r->policyname}",
            $offenders,
        );

        $this->assertSame([], $list, $msg);
    }

    /**
     * SECURITY regression test for the 2026-05-28 chr(0) sentinel bug.
     *
     * `silver.workspaces.workspaces_tenant_isolation` and
     * `silver.target_rationales.target_rationales_workspace_isolation`
     * used `NULLIF(current_setting('app.workspace_id', true), chr(0))`
     * as their "GUC unset" sentinel. `chr(0)` produces a TEXT containing
     * a U+0000 byte; PG18 rejects that (`null character not permitted`),
     * which causes the policy expression to fail CLOSED under psycopg2
     * even when it was meant to fail OPEN. Migration
     * 2026_05_29_190000_replace_broken_chr0_rls_policies replaced both
     * with the empty-string sentinel (`''`) — the same shape used by
     * the canonical workspace_isolation policies. This test stops the
     * chr(0) sentinel from regressing.
     */
    public function test_no_policy_uses_chr_zero_sentinel(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('RLS is Postgres-only.');
        }

        $offenders = DB::select(<<<'SQL'
            SELECT schemaname, tablename, policyname
              FROM pg_policies
             WHERE qual LIKE '%chr(0)%'
                OR with_check LIKE '%chr(0)%'
             ORDER BY schemaname, tablename, policyname
        SQL);

        $msg = 'RLS policies still use chr(0) as the "GUC unset" sentinel. '
            .'chr(0) produces a TEXT with a U+0000 byte which PG18 rejects with '
            .'`null character not permitted`, causing the policy to fail CLOSED '
            .'under psycopg2 even when it was meant to fail OPEN. Replace with '
            .'`NULLIF(current_setting(\'app.workspace_id\', true), \'\')` — see '
            .'2026_05_29_190000_replace_broken_chr0_rls_policies for the template.';

        $list = array_map(
            fn ($r) => "  {$r->schemaname}.{$r->tablename} → {$r->policyname}",
            $offenders,
        );

        $this->assertSame([], $list, $msg);
    }
}
