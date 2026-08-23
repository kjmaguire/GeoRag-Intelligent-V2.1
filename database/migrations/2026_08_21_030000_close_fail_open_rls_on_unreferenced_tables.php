<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Make RLS fail CLOSED on the workspace tables that nothing reads unbound.
 *
 * ## The shape being removed
 *
 * Two policy shapes coexist in this cluster. The correct one, on
 * silver.document_passages:
 *
 *     workspace_id = (NULLIF(current_setting('app.workspace_id', true), ''))::uuid
 *
 * An unset GUC yields NULL, NULL matches nothing, the query returns zero rows.
 * The other shape, measured 2026-08-21 across 93 policies on 92 tables:
 *
 *     (NULLIF(current_setting('app.workspace_id', true), '') IS NULL)
 *       OR (workspace_id = (NULLIF(current_setting('app.workspace_id', true), ''))::uuid)
 *
 * An unset GUC satisfies the FIRST branch, so every workspace's rows are
 * visible. That is what turned a bind bug into a cross-tenant write:
 * cluster_runner.py bound the GUC outside a transaction (SET LOCAL is
 * discarded there) and then resolved a project by slug with no workspace
 * predicate. `projects_slug_unique` is a GLOBAL unique index, so the lookup
 * could land on another tenant's project and the import's LAS curves and
 * collar coordinates were written into it. The bind bug is fixed (fail-loud
 * guard in app/db/scoped_pool.py plus the AST sweep in
 * src/fastapi/tests/test_bind_workspace_scope_sweep.py). The policy shape
 * that made it a cross-tenant WRITE rather than an empty result is not.
 *
 * ## Why only twelve tables
 *
 * A blanket flip of all 92 breaks the system, and not subtly. Two findings
 * from the 2026-08-21 census:
 *
 *  1. silver.projects is a BOOTSTRAP table. Both workspace resolvers read it
 *     precisely because they do not yet know the workspace:
 *     `_lookup_workspace_for_project()` in
 *     src/fastapi/app/services/workspace_resolution.py, and
 *     `BindWorkspaceRlsContext::resolveWorkspaceId()`, which runs BEFORE its
 *     own bind() call and therefore reads under the empty string that the
 *     previous request's finally-block left behind. Fail-closed makes both
 *     return nothing, so every project-scoped request resolves to no
 *     workspace and then sees zero rows. silver.workspaces is the same story
 *     — WorkspaceRlsCoverageTest already exempts it as "self-referential".
 *
 *  2. 86 connection-owning functions in src/fastapi/app query a fail-open
 *     table with no GUC bound. 21 of them carry an explicit
 *     `workspace_id = $n` predicate and are correct TODAY only because RLS
 *     lets them through; under fail-closed they return zero rows. The other
 *     65 have no workspace predicate at all — including every `_progress.py`
 *     heartbeat and status write, which update by run_id. Fail-closed turns
 *     those into silent zero-row UPDATEs and ingestion stops reporting
 *     progress.
 *
 * So this migration takes only the tables where the flip provably cannot
 * break anything: no unbound reader in src/fastapi/app, and no reference at
 * all in app/, database/seeders/, database/raw/, src/dagster/ or tests/.
 * Twelve of them. The remaining 80 need code changes first (bind the GUC, or
 * route the bootstrap lookups through a SECURITY DEFINER resolver), and that
 * sequencing is a human call.
 *
 * ## Owner exemption
 *
 * Seven of these twelve are not under FORCE ROW LEVEL SECURITY, so the owner
 * (`georag`, which MIGRATE_DB_USERNAME connects as) is exempt regardless of
 * policy text. Migrations, seeders and `psql -U georag` sessions are
 * therefore unaffected on those tables. The application role `georag_app` is
 * not the owner and IS subject to the policies, which is the path this
 * migration is about. FORCE is deliberately NOT added here — that would be a
 * second, independent behaviour change stacked on this one.
 *
 * Idempotent: DROP POLICY IF EXISTS then CREATE. Tables absent from the
 * cluster are skipped rather than failing the deploy, matching
 * 2026_08_21_020000.
 */
return new class extends Migration
{
    /** The canonical fail-closed scope. An unset GUC yields NULL. */
    private const SCOPE = "(NULLIF(current_setting('app.workspace_id', true), ''))::uuid";

    /**
     * table => [policy name, USING expr, WITH CHECK expr or null]
     *
     * Every expression below is the CURRENT policy with its fail-open branch
     * removed and nothing else changed.
     *
     * @return array<string, array{0: string, 1: string, 2: string|null}>
     */
    private function policies(): array
    {
        $scope = self::SCOPE;
        $eq = "workspace_id = {$scope}";

        return [
            // -- canonical shape, workspace_id NOT NULL ------------------
            'bronze.raw_collar_entries' => ['bronze_raw_collar_entries_workspace_isolation', $eq, null],
            'bronze.raw_geophysical_runs' => ['bronze_raw_geophysical_runs_workspace_isolation', $eq, null],
            'bronze.raw_surveys' => ['bronze_raw_surveys_workspace_isolation', $eq, null],
            'silver.control_points' => ['silver_control_points_workspace_isolation', $eq, null],
            'silver.historic_workings' => ['silver_historic_workings_workspace_isolation_v2', $eq, null],
            'silver.project_boundaries' => ['silver_project_boundaries_workspace_isolation_v2', $eq, null],
            'silver.sample_intervals' => ['sample_intervals_tenant_isolation', $eq, null],

            // -- `tenant_isolation` shape: carries an explicit WITH CHECK.
            // The original USING was
            //     NOT (workspace_id IS DISTINCT FROM <scope>)
            // which, on a NOT NULL workspace_id with the fail-open branches
            // gone, is exactly `=`. Normalised so all twelve read alike.
            'silver.ocr_page_quality' => ['tenant_isolation', $eq, $eq],
            'silver.parser_run_artifacts' => ['tenant_isolation', $eq, $eq],
            'silver.table_extraction_quality' => ['tenant_isolation', $eq, $eq],

            // -- no workspace_id column; scoped through its parent, which is
            // already fail-closed (silver_collab_anchors_workspace_isolation).
            'silver.collab_comments' => [
                'collab_comments_workspace_isolation',
                'EXISTS (SELECT 1 FROM silver.collab_anchors a '
                    .'WHERE a.anchor_id = collab_comments.anchor_id '
                    ."AND a.workspace_id = {$scope})",
                null,
            ],

            // -- workspace_id is NULLABLE here: NULL means a system-wide
            // sweep belonging to no tenant. Dropping that branch would hide
            // those rows from every reader, which is a different bug. Only
            // the unbound-GUC branch goes.
            'audit.audit_ledger_chain_fork_quarantine' => [
                'chain_fork_quarantine_workspace_isolation',
                "workspace_id IS NULL OR {$eq}",
                null,
            ],
        ];
    }

    public function up(): void
    {
        $this->apply(static fn (string $expr): string => $expr);
    }

    /**
     * Restore the fail-open branch, so this migration is reversible.
     */
    public function down(): void
    {
        $this->apply(static fn (string $expr): string => "(NULLIF(current_setting('app.workspace_id', true), '') IS NULL) OR ({$expr})");
    }

    /**
     * @param callable(string): string $wrap
     */
    private function apply(callable $wrap): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;  // RLS is a Postgres feature
        }

        foreach ($this->policies() as $table => [$policy, $using, $check]) {
            if (! $this->tableExists($table)) {
                continue;
            }

            $sql = "CREATE POLICY {$policy} ON {$table} USING ({$wrap($using)})";
            if ($check !== null) {
                $sql .= " WITH CHECK ({$wrap($check)})";
            }

            DB::statement("DROP POLICY IF EXISTS {$policy} ON {$table}");
            DB::statement($sql);
        }
    }

    /**
     * `to_regclass` returns NULL rather than raising for an absent relation,
     * so this is one round trip and no exception handling.
     */
    private function tableExists(string $qualified): bool
    {
        return DB::selectOne(
            'SELECT to_regclass(?) IS NOT NULL AS present',
            [$qualified],
        )?->present ?? false;
    }
};
