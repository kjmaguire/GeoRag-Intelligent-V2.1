<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;
use Tests\TestCase;

/**
 * Why `public.project_user` has no row-level security, and must not get the
 * usual policy.
 *
 * ## The observation that keeps getting made
 *
 * `project_user` is the pivot every tenancy check in this application
 * ultimately trusts: `User::hasProjectAccess()`, `User::isProjectOwner()`,
 * `BindWorkspaceRlsContext::workspacesFor()` and four Api/V1 controllers all
 * resolve access through it. It has no RLS at all. Said that way it reads
 * like the largest hole in the system, and it has now been written up as one
 * more than once.
 *
 * It is not a hole, and the obvious fix would take the platform down. This
 * file is here so the next person to notice finds the reasoning instead of
 * writing the migration.
 *
 * ## Why a policy cannot help
 *
 * Every read is already constrained to the authenticated user's own id.
 * `User::projects()` is a `belongsToMany`, so Eloquent emits
 * `... where project_user.user_id = <this user>` on every one of those paths.
 * A row-level policy cannot narrow a predicate that is already the narrowest
 * one available. There is no query in `app/` that reads the pivot unfiltered
 * except the boot guard in `AppServiceProvider`, which takes `limit(1)` and
 * discards the result purely to prove the table is readable.
 *
 * ## Why the obvious policy would be an outage
 *
 * The table has no `workspace_id` column, so the canonical shape — compare
 * `workspace_id` against `current_setting('app.workspace_id')` — is not even
 * expressible; it would have to subquery `silver.projects`, which is itself
 * RLS-protected.
 *
 * Worse, it is self-referential in the same way `silver.workspaces` is, and
 * that table is already a documented permanent exemption in
 * WorkspaceRlsCoverageTest for exactly this reason.
 * `BindWorkspaceRlsContext::workspacesFor()` READS `project_user` in order to
 * DERIVE the workspace it is about to bind into `app.workspace_id`. So:
 *
 *   - a fail-closed policy returns zero rows on an unbound connection, every
 *     user resolves to zero workspaces, nothing ever gets bound, and every
 *     authenticated request 403s or 503s. Total outage, and it would look
 *     like an auth bug rather than an RLS one;
 *   - a fail-open policy (the `NULLIF(...) IS NULL OR ...` shape used across
 *     this codebase) admits every row whenever the GUC is unset — which is
 *     precisely the moment the pivot is read. It would add a policy that
 *     never applies, and the appearance of coverage is worse than the honest
 *     absence of it.
 *
 * ## What would actually harden it
 *
 * Bind a second GUC — `app.user_id`, from the session rather than from the
 * pivot — and policy `project_user` on `user_id = current_setting(...)`.
 * That breaks the cycle because the value no longer comes from the table
 * being protected. It is a real design change: a new GUC, middleware on both
 * the Laravel and FastAPI sides, and a decision about what happens to
 * artisan and queue contexts that have no user. It belongs in an ADR, not in
 * a migration written to close a finding.
 *
 * The assertions below pin the facts that argument rests on, so it fails
 * rather than rots if any of them stops being true.
 */
final class ProjectUserPivotRlsExemptionTest extends TestCase
{
    /**
     * The canonical policy shape needs this column. It is not there, which is
     * why "just add the standard tenant_isolation policy" is not an option.
     */
    public function test_the_pivot_has_no_workspace_id_to_write_a_policy_against(): void
    {
        // This asks a question about the schema the migrations build, so it
        // needs a migrated database. The file is registered in
        // phpunit.pgsql.xml's read-only suite, which runs after the
        // RefreshDatabase group has populated the cluster; under the SQLite
        // fast suite there is no schema to inspect at all.
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('Needs the migrated Postgres schema.');
        }

        $this->assertTrue(
            Schema::hasTable('project_user'),
            'The pivot is gone. AppServiceProvider refuses to boot without it, '
            .'so this is a bigger problem than this test.',
        );

        $this->assertFalse(
            Schema::hasColumn('project_user', 'workspace_id'),
            'project_user has grown a workspace_id column. The reasoning in '
            .'this file assumed it had none, so the canonical tenant_isolation '
            .'policy is now expressible and this exemption should be revisited '
            .'— but read the self-reference argument above first: '
            .'BindWorkspaceRlsContext reads this table to DERIVE the GUC a '
            .'policy would test, and a column does not fix that.',
        );
    }

    /**
     * If RLS is ever switched on here, it must be a deliberate act with the
     * argument above answered — not a sweep picking the table up.
     */
    public function test_rls_is_deliberately_off(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('RLS is Postgres-only.');
        }

        $row = DB::selectOne(<<<'SQL'
            SELECT c.relrowsecurity AS enabled
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relname = 'project_user'
        SQL);

        $this->assertNotNull($row, 'public.project_user is missing from pg_class.');

        $this->assertFalse(
            (bool) $row->enabled,
            'RLS is now enabled on public.project_user. If that was deliberate, '
            .'delete this test and say in the migration how '
            .'BindWorkspaceRlsContext::workspacesFor() still resolves a '
            .'workspace for a user on a connection with no bound GUC — it reads '
            .'this table to derive the very value the policy tests. If it was a '
            .'sweep, this is the outage that sweep is about to cause.',
        );
    }

    /**
     * The safety property is in the QUERIES, not the schema: every read is
     * already scoped to one user. A raw read of the pivot somewhere new is
     * the thing that would change the answer, so it is what this watches for.
     */
    public function test_every_application_read_of_the_pivot_is_user_scoped(): void
    {
        $repo = dirname(__DIR__, 3);

        /**
         * The only file allowed to query the pivot directly, and why.
         *
         * Exactly one entry, and that is deliberate. `User.php` defines the
         * belongsToMany and `BindWorkspaceRlsContext` goes through it, so
         * neither trips the predicate below — listing them here would be a
         * stale exemption, which is the shape that hides the next real one.
         * The assertion after the scan fails if an entry stops being needed.
         *
         * @var array<string, string>
         */
        $allowed = [
            // DB::table('project_user')->limit(1)->get(), result discarded —
            // it proves the table is readable and refuses to boot otherwise.
            'app/Providers/AppServiceProvider.php' => 'boot-time readability probe, result discarded',
        ];

        $offenders = [];
        $allowedThatMatched = [];
        foreach ($this->phpFilesUnder($repo.'/app') as $path) {
            $relative = str_replace($repo.'/', '', $path);
            $contents = (string) file_get_contents($path);
            if (isset($allowed[$relative])) {
                if ($this->queriesThePivotDirectly($contents)) {
                    $allowedThatMatched[] = $relative;
                }

                continue;
            }
            // Only a QUERY is interesting. Comments naming the pivot are how
            // the controllers document what they gate on, and there are a
            // dozen of them.
            if ($this->queriesThePivotDirectly($contents)) {
                $offenders[] = $relative;
            }
        }

        $this->assertSame(
            [],
            $offenders,
            "These query project_user directly rather than through \$user->projects():\n  "
            .implode("\n  ", $offenders)
            ."\n\nThat matters because the pivot has NO row-level security, and the "
            .'reason that is safe is that every existing read is already '
            ."constrained to the authenticated user's own id by the belongsToMany. "
            .'A direct query is not, and nothing in the database will stop it '
            .'returning another user\'s memberships. Either scope it explicitly '
            .'or add it to the allow-list in this test with the reason.',
        );

        $this->assertSame(
            array_keys($allowed),
            $allowedThatMatched,
            'The allow-list in this test no longer matches what actually '
            .'queries the pivot. An entry that has stopped querying it is a '
            .'stale exemption — it would silently cover the next file that '
            .'starts. Remove it.',
        );
    }

    private function queriesThePivotDirectly(string $contents): bool
    {
        return preg_match(
            '/(?:DB::table|->from|join|JOIN)\s*\(?\s*[\'"]project_user[\'"]/i',
            $contents,
        ) === 1;
    }

    /** @return list<string> */
    private function phpFilesUnder(string $root): array
    {
        $files = [];
        $walker = new \RecursiveIteratorIterator(
            new \RecursiveDirectoryIterator($root, \FilesystemIterator::SKIP_DOTS),
        );
        foreach ($walker as $file) {
            if ($file->isFile() && $file->getExtension() === 'php') {
                $files[] = $file->getPathname();
            }
        }
        sort($files);

        return $files;
    }
}
