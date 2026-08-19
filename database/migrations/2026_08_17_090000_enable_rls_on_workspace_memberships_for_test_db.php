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
