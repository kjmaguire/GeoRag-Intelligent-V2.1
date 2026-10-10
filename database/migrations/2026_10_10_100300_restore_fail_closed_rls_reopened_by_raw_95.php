<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Put the fail-closed `tenant_isolation` policies back on five tables that
 * database/raw/phase0/95-rls-policies.sql re-opened on every deploy (database
 * audit 2026-10, MEDIUM / security).
 *
 * ## What happened
 *
 * The migration chain made these tables strict:
 *
 *   workspace.workspace_memberships    2026_08_17_090000
 *   workspace.workspace_agent_config   2026_08_28_100100
 *   workspace.dry_run_outputs          2026_08_28_100100
 *   outbox.pending_propagations        2026_08_28_100000
 *   outbox.propagation_attempts        2026_08_28_100000
 *
 * (2026_08_14_030000 listed all five as safe to close, but skipped them because
 * they did not exist yet; the later migrations created them already closed.)
 * The ECS migrate task runs `php artisan migrate` THEN `php artisan db:apply-raw`,
 * and 95's DO-block DROPs `tenant_isolation` on a hard-coded list of tables and
 * re-creates it in the fail-open shape
 *
 *   ... OR current_setting('app.workspace_id', true) IS NULL
 *       OR current_setting('app.workspace_id', true) = ''
 *
 * so every deploy undid the migrations for exactly these five. An unbound
 * `georag_app` session (any code path that forgot to bind, any SQL injection
 * that reaches a connection without a workspace) then read every tenant's
 * memberships, agent configuration, dry-run payloads and outbox payloads.
 *
 * Fresh database -> restored by this migration at the next `migrate`; existing
 * database -> this migration is what repairs it, because editing 95 alone would
 * leave the policy 95 already wrote in place (95 no longer touches these tables
 * and the 2026_08_14 / 2026_08_28 migrations have already run). 95 is edited in
 * the same change so the next `db:apply-raw` does not reopen them again.
 *
 * ## The two shapes
 *
 * The three `workspace.*` tables have `workspace_id NOT NULL`, so the 2026_08_14
 * shape is exact:
 *
 *   workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
 *
 * The two `outbox.*` tables have a NULLABLE `workspace_id`. A NULL row is a
 * PLATFORM row: the tenant-isolation auditor's security escalation is enqueued
 * with workspace_id NULL, and outbox_dispatcher / nightly_ingestion_integrity
 * claim and re-queue those rows in a dedicated pass that clears the workspace
 * scope (HAT-1, 2026-09-29; manual ch. 07 s2.4). The strict `=` shape above can
 * match no NULL row at all -- `NULL = NULL` is not true -- so under it the
 * platform pass claims nothing and the escalation can neither be enqueued
 * (WITH CHECK fails) nor drained. This was measured, not assumed: with the `=`
 * shape on these two tables
 * src/fastapi/tests/test_cron_sweeps_under_app_role.py::
 * test_the_outbox_claims_tenant_and_platform_rows fails on `platform_row in set()`.
 * They therefore get
 *
 *   workspace_id IS NOT DISTINCT FROM NULLIF(current_setting('app.workspace_id', true), '')::uuid
 *
 * which is the dispatcher's own scope predicate and carries NO unbound-GUC
 * branch: a session bound to workspace W sees and writes W's rows only; a session
 * with no workspace bound sees and writes PLATFORM rows only, never a tenant's.
 * Relative to what production has been running (95's fail-open shape) that is a
 * strict narrowing; relative to the migrations' `=` shape it admits the one
 * thing the sweeps need, NULL-workspace rows to an unbound session, and nothing
 * else.
 *
 * ## Deliberately NOT here: usage.usage_events, usage.workspace_cost_ceilings
 *
 * The finding names those two as well (95 adds a second, fail-open
 * `tenant_isolation` beside their strict `usage_*_tenant_isolation`). They are
 * left as they are until the code that reads and writes them without a bound
 * workspace is fixed, because tightening them today silently stops cost
 * metering: the chat persist path INSERTs usage_events on a bare pooled
 * connection (src/fastapi/app/agent/agentic_retrieval/nodes.py
 * `_write_chat_usage_event`), the @georag_agent wrapper does the same
 * (agents/wrapper.py `_write_usage_event`), and model_cost_summary reads both
 * tables across every tenant unbound. See the PR notes; database/raw/phase0/95
 * records the same reason next to the entries that remain.
 *
 * ## Readers checked before tightening
 *
 * outbox_dispatcher, nightly_ingestion_integrity tier 4 and cost_burn_watcher
 * already bind per workspace (plus a cleared-scope platform pass), tool_gateway
 * binds via scoped_connection, and nothing in app/ (Laravel) touches these
 * tables. Still unbound, and degraded rather than broken by this change: the
 * support_packet agent's outbox enqueue for a TENANT row (agents/phase0/
 * support_packet.py), store_reconciliation_run and reliability_metrics_publisher
 * (they read outbox rows across tenants; they now see platform rows only), and
 * the unused wrapper `_record_dry_run`. None is a tenant-isolation concern; all
 * are listed in the PR notes for the owners of those files.
 *
 * FORCE ROW LEVEL SECURITY is (re)asserted: without it the table owner bypasses
 * the policy.
 *
 * Reversible: down() restores 95's fail-open shape, which is what these tables
 * carried before.
 */
return new class extends Migration
{
    private const STRICT = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid";

    private const PLATFORM_AWARE = "workspace_id IS NOT DISTINCT FROM NULLIF(current_setting('app.workspace_id', true), '')::uuid";

    /**
     * qualified table => policy expression (USING and WITH CHECK).
     *
     * @var array<string, string>
     */
    private const TABLES = [
        'workspace.workspace_memberships' => self::STRICT,
        'workspace.workspace_agent_config' => self::STRICT,
        'workspace.dry_run_outputs' => self::STRICT,
        'outbox.pending_propagations' => self::PLATFORM_AWARE,
        'outbox.propagation_attempts' => self::PLATFORM_AWARE,
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (self::TABLES as $qualified => $expression) {
            if (! $this->tableExists($qualified)) {
                continue;
            }

            $this->asTableOwner($qualified, static function () use ($qualified, $expression): void {
                DB::statement("ALTER TABLE {$qualified} ENABLE ROW LEVEL SECURITY");
                DB::statement("ALTER TABLE {$qualified} FORCE ROW LEVEL SECURITY");
                DB::statement("DROP POLICY IF EXISTS tenant_isolation ON {$qualified}");
                DB::statement(
                    "CREATE POLICY tenant_isolation ON {$qualified}
                        USING ({$expression})
                        WITH CHECK ({$expression})",
                );
            });
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // database/raw/phase0/95-rls-policies.sql's macro shape, verbatim.
        $failOpen = "workspace_id IS NOT DISTINCT FROM NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    OR current_setting('app.workspace_id', true) IS NULL
                    OR current_setting('app.workspace_id', true) = ''";

        foreach (array_keys(self::TABLES) as $qualified) {
            if (! $this->tableExists($qualified)) {
                continue;
            }

            $this->asTableOwner($qualified, static function () use ($qualified, $failOpen): void {
                DB::statement("DROP POLICY IF EXISTS tenant_isolation ON {$qualified}");
                DB::statement(
                    "CREATE POLICY tenant_isolation ON {$qualified}
                        USING ({$failOpen})
                        WITH CHECK ({$failOpen})",
                );
            });
        }
    }

    private function tableExists(string $qualified): bool
    {
        return (bool) DB::selectOne('SELECT to_regclass(?) IS NOT NULL AS present', [$qualified])->present;
    }

    /**
     * Run $work with the table's owning role assumed, when that is needed and
     * possible. ENABLE/FORCE ROW LEVEL SECURITY and CREATE POLICY all require
     * ownership, and workspace.workspace_memberships is created by raw SQL on
     * clusters that predate the migration chain, so its owner is not
     * necessarily the migrating role (2026_08_17_090000 hit exactly that).
     *
     * Deliberately does NOT skip silently when ownership cannot be obtained: RLS
     * is a tenancy control, and leaving a table open on the one environment that
     * holds tenant data is worse than a failed deploy.
     */
    private function asTableOwner(string $qualified, callable $work): void
    {
        $owner = DB::selectOne(
            'SELECT pg_get_userbyid(c.relowner) AS owner FROM pg_class c WHERE c.oid = to_regclass(?)',
            [$qualified],
        )?->owner;
        $current = DB::selectOne('SELECT current_user AS role')?->role;

        if ($owner === null || $owner === $current) {
            $work();

            return;
        }

        $canAssume = (bool) (DB::selectOne(
            "SELECT pg_has_role(current_user, ?, 'MEMBER') AS ok",
            [$owner],
        )?->ok ?? false);

        if (! $canAssume) {
            throw new RuntimeException(sprintf(
                'Cannot restore the fail-closed policy on %s: it is owned by "%s" and "%s" is not a member of that '
                .'role. Grant membership once with: GRANT "%s" TO "%s"; -- or reassign: ALTER TABLE %s OWNER TO "%s";',
                $qualified, $owner, $current, $owner, $current, $qualified, $current,
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
