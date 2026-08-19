<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * 2026-08-17 CI-gap audit — `WorkspaceRlsCoverageTest` never actually ran
 * in CI (see `.github/workflows/ci.yml` laravel job / `phpunit.pgsql.xml`
 * header), so its real failure went undetected: two workspace-scoped
 * tables carry a `workspace_id` column but never got `ENABLE ROW LEVEL
 * SECURITY` + a policy, despite both being explicitly documented as
 * intended to have one:
 *
 *   - `targeting.target_backtests` — its own creating migration
 *     (2026_05_13_100000_create_targeting_schema.php) states "All tables
 *     RLS-protected via app.workspace_id session setting, same pattern as
 *     silver.* phase3 tables" for the whole file, but this table alone
 *     never got the ENABLE ROW LEVEL SECURITY statement. Its `workspace_id`
 *     is deliberately NULLABLE (platform-wide backtest rows are legitimate
 *     — see WorkspaceRlsCoverageTest's own docblock on
 *     test_second_verified_subset_has_no_fail_open_escape_hatch, which
 *     already documents it as intentionally fail-open) — same nullable
 *     three-clause shape as gold.mv_refresh_log.
 *
 *   - `workspace.workspace_roles` — its creating raw SQL
 *     (database/raw/phase0/10-layer-a-workspace-foundation.sql) documents
 *     "workspace_id NULL ⇒ global role; otherwise workspace-scoped" but
 *     Phase 0's RLS sweep only covered the sibling
 *     workspace.workspace_memberships table, missing this one. Global
 *     (NULL workspace_id) system roles must stay readable from every
 *     workspace, so this also needs the nullable three-clause shape, not
 *     the strict two-clause one.
 *
 * (`ops.support_tickets`, the third gap the audit found, is NOT touched
 * here — its own migration, 2026_05_13_140100_create_ops_support_schema,
 * explicitly documents "ops.* schema is GLOBAL — no workspace RLS;
 * cross-workspace access is logged via app.audit.emit_audit... per
 * §25.3." Adding RLS there would contradict that design decision; it's
 * added to WorkspaceRlsCoverageTest::EXEMPT_TABLES instead.)
 *
 * Same nullable-workspace policy shape as
 * 2026_05_25_173814_enable_rls_on_post_phase0_workspace_tables's
 * enableNullableWorkspacePolicy() (fail-open on unset GUC, matching every
 * other not-yet-fail-closed table in this codebase — see
 * WorkspaceRlsCoverageTest's own docblock on why a blanket fail-closed
 * assertion isn't appropriate here).
 *
 * SQLite (test DB fast suite) does not support RLS — gated on Postgres.
 */
return new class extends Migration
{
    /**
     * @var list<array{schema: string, table: string}>
     */
    private const NULLABLE_TABLES = [
        ['schema' => 'targeting', 'table' => 'target_backtests'],
        ['schema' => 'workspace', 'table' => 'workspace_roles'],
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (self::NULLABLE_TABLES as $t) {
            $this->enableNullableWorkspacePolicy($t['schema'], $t['table']);
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (self::NULLABLE_TABLES as $t) {
            $qualified = "{$t['schema']}.{$t['table']}";
            $policy = $this->policyName($t['schema'], $t['table']);
            DB::statement("DROP POLICY IF EXISTS {$policy} ON {$qualified}");
            DB::statement("ALTER TABLE {$qualified} DISABLE ROW LEVEL SECURITY");
        }
    }

    private function policyName(string $schema, string $table): string
    {
        return "{$schema}_{$table}_workspace_isolation";
    }

    private function enableNullableWorkspacePolicy(string $schema, string $table): void
    {
        $qualified = "{$schema}.{$table}";
        $policy = $this->policyName($schema, $table);

        // ENABLE ROW LEVEL SECURITY and CREATE POLICY both require table
        // OWNERSHIP — not merely privileges on it. That broke the 2026-08-19
        // deploy:
        //
        //   SQLSTATE[42501]: must be owner of table workspace_roles
        //   (SQL: ALTER TABLE workspace.workspace_roles
        //         ENABLE ROW LEVEL SECURITY)
        //
        // workspace.workspace_roles and workspace.workspace_memberships are
        // created on real deployments by
        // database/raw/phase0/10-layer-a-workspace-foundation.sql, applied
        // outside the migration chain, so they are owned by whichever role ran
        // that bootstrap — not by `georag`, which is who migrations run as.
        // targeting.target_backtests is migration-created and therefore
        // georag-owned, which is why this file passed everywhere it was tested
        // and failed only against a cluster that had the raw SQL applied.
        //
        // Assume the owning role for the duration when we are a member of it.
        // That is the ordinary Postgres answer and needs no elevated grant —
        // georag is already a member of the bootstrap role on deployments
        // where that role created these tables.
        $this->asTableOwner($schema, $table, function () use ($qualified, $policy): void {
            DB::statement("ALTER TABLE {$qualified} ENABLE ROW LEVEL SECURITY");
            DB::statement("DROP POLICY IF EXISTS {$policy} ON {$qualified}");
            DB::statement(<<<SQL
                CREATE POLICY {$policy} ON {$qualified}
                  USING (
                    NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                    OR workspace_id IS NULL
                    OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                  )
            SQL);
        });
    }

    /**
     * Run $work with the table's owning role assumed, when that is necessary
     * and possible.
     *
     * Deliberately does NOT skip silently when ownership cannot be obtained.
     * RLS is a tenancy control; quietly leaving it off on the one environment
     * that actually holds tenant data is a worse outcome than a failed deploy,
     * and it is precisely the "looked green, protected nothing" shape the
     * 2026-08-19 audit kept turning up. Fail loudly, and say what to run.
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
            // Table genuinely absent — the caller's tableExists() guard already
            // covers the intended case; nothing to protect here.
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

        // SET ROLE rather than SET LOCAL ROLE so this behaves identically
        // whether or not the migration runs inside a transaction, with an
        // explicit reset in finally so a failure inside $work cannot leave the
        // session wearing the wrong role.
        DB::statement('SET ROLE '.$this->quoteIdentifier($owner));

        try {
            $work();
        } finally {
            DB::statement('RESET ROLE');
        }
    }

    private function quoteIdentifier(string $identifier): string
    {
        return '"'.str_replace('"', '""', $identifier).'"';
    }
};
