<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * targeting.target_backtests: an unset workspace GUC no longer admits every
 * tenant's rows (security audit, 2026-09-29; §06b).
 *
 * Two PERMISSIVE policies could sit on this table, and permissive policies
 * are OR-ed:
 *
 *   - `target_backtests_workspace_isolation`, from
 *     database/raw/phase0/98-rls-tenant-isolation-block3.sql — fail-closed,
 *     `workspace_id = NULLIF(GUC, '')::uuid`;
 *   - `targeting_target_backtests_workspace_isolation`, from
 *     2026_08_17_060000_enable_rls_on_target_backtests_and_workspace_roles —
 *     `NULLIF(GUC, '') IS NULL OR workspace_id IS NULL OR workspace_id = GUC`.
 *
 * Wherever the raw layer is applied both exist, and the second one's
 * "GUC unset" branch makes the first irrelevant: a session with no
 * app.workspace_id reads every workspace's backtests. Where only the
 * migration chain runs, the fail-open one is the only policy.
 *
 * What stays: rows with a NULL workspace_id are platform-wide backtests and
 * remain readable from every workspace (the reason 2026_08_17 made the column
 * nullable-aware). What goes: the unbound-GUC escape hatch. Writes must name
 * the bound workspace; nothing writes platform rows through the app role
 * (field_outcome_learning binds its workspace and writes it).
 */
return new class extends Migration
{
    private const TABLE = 'targeting.target_backtests';

    private const POLICY = 'targeting_target_backtests_workspace_isolation';

    private const RAW_POLICY = 'target_backtests_workspace_isolation';

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql' || ! $this->tableExists()) {
            return;
        }

        $this->asTableOwner(function (): void {
            $t = self::TABLE;
            DB::statement("ALTER TABLE {$t} ENABLE ROW LEVEL SECURITY");
            DB::statement("ALTER TABLE {$t} FORCE ROW LEVEL SECURITY");
            DB::statement('DROP POLICY IF EXISTS '.self::RAW_POLICY." ON {$t}");
            DB::statement('DROP POLICY IF EXISTS '.self::POLICY." ON {$t}");
            DB::statement(
                'CREATE POLICY '.self::POLICY." ON {$t}
                    USING (
                        workspace_id IS NULL
                        OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    )
                    WITH CHECK (
                        workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    )",
            );
            // The policy filters on workspace_id; the raw layer indexes it,
            // the migration chain never did.
            DB::statement("CREATE INDEX IF NOT EXISTS idx_target_backtests_workspace_id ON {$t} (workspace_id)");
        });
    }

    /**
     * Restores the 2026_08_17 shape (not the raw one), which is what a
     * migrate-only cluster had.
     */
    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql' || ! $this->tableExists()) {
            return;
        }

        $this->asTableOwner(function (): void {
            $t = self::TABLE;
            DB::statement('DROP POLICY IF EXISTS '.self::POLICY." ON {$t}");
            DB::statement(
                'CREATE POLICY '.self::POLICY." ON {$t}
                    USING (
                        NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                        OR workspace_id IS NULL
                        OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    )",
            );
        });
    }

    private function tableExists(): bool
    {
        return (bool) (DB::selectOne('SELECT to_regclass(?) IS NOT NULL AS present', [self::TABLE])?->present ?? false);
    }

    /**
     * ALTER TABLE / CREATE POLICY need ownership. Migration-created, so
     * normally the migrating role owns it; assume the owner when we are a
     * member of it, as 2026_08_17_060000 does.
     */
    private function asTableOwner(Closure $work): void
    {
        $owner = DB::selectOne(
            'SELECT pg_get_userbyid(c.relowner) AS owner FROM pg_class c WHERE c.oid = to_regclass(?)',
            [self::TABLE],
        )?->owner;
        $current = DB::selectOne('SELECT current_user AS role')?->role;

        if ($owner === null || $owner === $current) {
            $work();

            return;
        }

        if (! (bool) (DB::selectOne("SELECT pg_has_role(current_user, ?, 'MEMBER') AS ok", [$owner])?->ok ?? false)) {
            throw new RuntimeException(sprintf(
                '%s is owned by %s and %s is not a member of it; cannot rewrite its RLS policy.',
                self::TABLE, $owner, $current,
            ));
        }

        DB::statement('SET ROLE "'.str_replace('"', '""', (string) $owner).'"');
        try {
            $work();
        } finally {
            DB::statement('RESET ROLE');
        }
    }
};
