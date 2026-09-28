<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Re-create integration_credentials_audit_workspace_isolation with the empty
 * GUC guarded, on databases that already ran 2026_08_28_100700.
 *
 * That migration ported the policy from 98-rls-tenant-isolation-block3.sql
 * with a bare `current_setting('app.workspace_id', true)::uuid`, which raises
 * `22P02` when BindWorkspaceRlsContext has bound the '' sentinel. Block 3 and
 * 2026_08_28_100700 now both use the NULLIF form; this carries the fix to a
 * database that migrated before the edit and has not re-run `db:apply-raw`
 * (compose dev, CI, feature-test databases). On AWS the raw pass already
 * re-creates it every deploy, so there this is a no-op in effect.
 *
 * The policy stays strict: '' becomes NULL, which matches no row. It does
 * not gain an `IS NULL OR` branch.
 */
return new class extends Migration
{
    private const TABLE = 'audit.integration_credentials_audit';

    private const POLICY = 'integration_credentials_audit_workspace_isolation';

    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;  // RLS is a Postgres feature
        }

        if (! $this->tableExists(self::TABLE)) {
            return;
        }

        $scope = "NULLIF(current_setting('app.workspace_id', true), '')::uuid";

        DB::statement('DROP POLICY IF EXISTS '.self::POLICY.' ON '.self::TABLE);
        DB::statement(
            'CREATE POLICY '.self::POLICY.' ON '.self::TABLE
                ." USING (workspace_id = {$scope})"
                ." WITH CHECK (workspace_id = {$scope})",
        );
    }

    /**
     * Nothing to undo. The previous shape 500s on the '' sentinel, and
     * 2026_08_28_100700 now creates this same policy, so rolling back to it
     * would reintroduce the bug rather than restore a working state.
     */
    public function down(): void {}

    /**
     * `to_regclass` returns NULL rather than raising for an absent relation.
     */
    private function tableExists(string $qualified): bool
    {
        return DB::selectOne(
            'SELECT to_regclass(?) IS NOT NULL AS present',
            [$qualified],
        )?->present ?? false;
    }
};
