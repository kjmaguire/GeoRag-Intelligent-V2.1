<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * 2026-08-17 CI-gap audit — closes a gap `2026_08_15_030400_provision_
 * workspace_roles_and_memberships_for_test_db.php`'s own docblock already
 * flagged as a known follow-up (see that file's "RLS is deliberately NOT
 * added here" section).
 *
 * `workspace.workspace_memberships` is created ONLY by
 * database/raw/phase0/10-layer-a-workspace-foundation.sql in production;
 * `2026_08_15_030400` transcribed that table for a migrate-only Postgres
 * test DB, but ran too late (by filename order) for the two migrations
 * that actually apply RLS to it — `2026_05_25_185013_normalize_layered_
 * workspace_isolation_policies_phase2` and `2026_08_14_030000_close_rls_
 * admin_escape_hatch_verified_subset` — both dated earlier and both
 * guarded by a `tableExists()` check that silently no-ops when the table
 * doesn't exist yet. Net effect: on a fresh migrate-only test DB, the
 * table exists but has never had RLS enabled at all.
 *
 * This was invisible until now because `workspace` was missing from
 * phpunit.pgsql.xml's `DB_SEARCH_PATH` (fixed alongside this migration —
 * see that file's history), so `migrate:fresh` never actually dropped and
 * rebuilt this schema between runs; a long-lived local test DB could
 * still be carrying RLS applied by an earlier, differently-ordered manual
 * run.
 *
 * Applies the exact canonical fail-closed `tenant_isolation` policy shape
 * from `2026_08_14_030000` (see that migration's docblock for why this
 * table is in the fail-closed "verified subset" rather than the more
 * common fail-open shape) — kept as its own follow-up migration, dated
 * after `2026_08_15_030400`, rather than reordering/editing either
 * historical migration.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (! $this->tableExists('workspace', 'workspace_memberships')) {
            return;
        }

        // Same ownership problem as 2026_08_17_060000 — see that file's
        // asTableOwner() docblock. ENABLE/FORCE ROW LEVEL SECURITY and
        // CREATE POLICY all require table OWNERSHIP, and
        // workspace.workspace_memberships is created on real deployments by
        // database/raw/phase0/10-layer-a-workspace-foundation.sql, outside the
        // migration chain, so it is not owned by `georag`. This migration runs
        // immediately after 060000 and would have failed the same way against
        // Azure the moment 060000 was fixed.
        $this->asTableOwner('workspace', 'workspace_memberships', static function (): void {
            DB::statement('ALTER TABLE workspace.workspace_memberships ENABLE ROW LEVEL SECURITY');
            DB::statement('ALTER TABLE workspace.workspace_memberships FORCE ROW LEVEL SECURITY');

            DB::statement('DROP POLICY IF EXISTS tenant_isolation ON workspace.workspace_memberships');
            DB::statement(<<<'SQL'
                CREATE POLICY tenant_isolation ON workspace.workspace_memberships
                    USING (
                        workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    )
                    WITH CHECK (
                        workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    )
            SQL);
        });
    }

    /**
     * Run $work with the table's owning role assumed, when necessary and
     * possible. Mirrors 2026_08_17_060000's helper — duplicated rather than
     * shared because migrations must stay self-contained and replayable
     * independently of each other.
     *
     * Deliberately does NOT skip silently when ownership cannot be obtained:
     * RLS is a tenancy control, and leaving it off on the one environment that
     * holds tenant data is worse than a failed deploy.
     */
    private function asTableOwner(string $schema, string $table, callable $work): void
    {
        $owner = DB::selectOne(
            'SELECT pg_get_userbyid(c.relowner) AS owner
             FROM pg_class c
             JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = ? AND c.relname = ?',
            [$schema, $table],
        )?->owner;

        if ($owner === null) {
            return;
        }

        $current = DB::selectOne('SELECT current_user AS role')?->role;

        if ($owner === $current) {
            $work();

            return;
        }

        $canAssume = (bool) (DB::selectOne(
            "SELECT pg_has_role(current_user, ?, 'MEMBER') AS ok",
            [$owner],
        )?->ok ?? false);

        if (! $canAssume) {
            throw new RuntimeException(sprintf(
                'Cannot enable RLS on %s.%s: it is owned by "%s" and "%s" is not '
                .'a member of that role. Grant membership once with: '
                .'GRANT "%s" TO "%s"; -- or reassign: ALTER TABLE %s.%s OWNER TO "%s";',
                $schema, $table, $owner, $current,
                $owner, $current, $schema, $table, $current,
            ));
        }

        DB::statement('SET ROLE "'.str_replace('"', '""', $owner).'"');

        try {
            $work();
        } finally {
            DB::statement('RESET ROLE');
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (! $this->tableExists('workspace', 'workspace_memberships')) {
            return;
        }

        DB::statement('DROP POLICY IF EXISTS tenant_isolation ON workspace.workspace_memberships');
        DB::statement('ALTER TABLE workspace.workspace_memberships DISABLE ROW LEVEL SECURITY');
    }

    private function tableExists(string $schema, string $table): bool
    {
        return (bool) DB::selectOne(
            'select to_regclass(?) is not null as exists_',
            ["{$schema}.{$table}"],
        )->exists_;
    }
};
