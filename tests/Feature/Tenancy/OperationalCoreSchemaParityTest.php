<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Illuminate\Support\Facades\DB;
use PHPUnit\Framework\Attributes\Test;
use Tests\TestCase;

/**
 * Pins the fifteen operational-core objects ported out of `database/raw/` and
 * into the migration chain on 2026-08-28.
 *
 * ## Why a test rather than trusting the migration
 *
 * These tables had existed only in `database/raw/`, which CD never runs —
 * `cd.yml` executes `php artisan migrate` and nothing else, and
 * `php artisan db:apply-raw` is a manual operator step. So for months
 * `outbox_dispatcher` polled a table that was not there on Azure once a
 * minute, and the nightly `idempotency_keys_cleanup` deleted from another.
 * `scripts/check-raw-migration-parity.php` tells you an object is unmanaged;
 * only this test tells you the port actually produced the right thing.
 *
 * ## What it asserts, and why each part matters
 *
 * 1. Every object exists — the parity gate proves a migration CREATEs it, not
 *    that the statement succeeds.
 * 2. The four tables in the verified fail-closed subset really are closed.
 *    `2026_08_14_030000_close_rls_admin_escape_hatch_verified_subset` skipped
 *    them because they did not exist, so
 *    `WorkspaceRlsCoverageTest::test_verified_subset_has_no_fail_open_escape_hatch`
 *    has been passing vacuously for them. Creating them fail-open would have
 *    turned that into a real failure; this asserts the closed shape directly.
 * 3. The three global-config tables have NO workspace_id and NO RLS —
 *    `phase0/95-rls-policies.sql` names them under "Tables that DO NOT get
 *    RLS", so their absence from the RLS set is deliberate and worth pinning
 *    against a well-meaning future sweep.
 * 4. The `feature_flags_audit` trigger fires and skips no-op updates.
 *
 * `test_every_rls_enabled_table_is_forced` in `WorkspaceRlsCoverageTest`
 * already covers FORCE globally, so it is not duplicated here.
 *
 * Postgres-only: RLS and `NULLS NOT DISTINCT` have no SQLite equivalent.
 * Registered in `phpunit.pgsql.xml` — `PgsqlSuiteManifestTest` fails if a
 * Postgres-gated file under tests/Feature is missing from that list.
 */
final class OperationalCoreSchemaParityTest extends TestCase
{
    /** Every object the 2026_08_28_1003xx migrations create. */
    private const PORTED_TABLES = [
        'outbox.pending_propagations',
        'outbox.propagation_attempts',
        'workspace.agent_timeouts',
        'workspace.prompt_versions',
        'workspace.agent_prompt_pins',
        'workspace.workspace_agent_config',
        'workspace.idempotency_keys',
        'workspace.dry_run_outputs',
        'workspace.agent_risk_tiers',
        'workspace.agent_permissions',
        'workspace.approval_requirements',
        'workspace.tool_invocations',
        'workspace.feature_flags',
        'workspace.feature_flag_history',
    ];

    /**
     * Mirrors the subset in 2026_08_14_030000 that this port had to honour.
     * Policy name is `tenant_isolation` for all four.
     */
    private const FAIL_CLOSED = [
        'outbox.pending_propagations',
        'outbox.propagation_attempts',
        'workspace.workspace_agent_config',
        'workspace.dry_run_outputs',
    ];

    /** Global config: no workspace_id column, therefore no RLS. */
    private const GLOBAL_CONFIG = [
        'workspace.agent_timeouts',
        'workspace.prompt_versions',
        'workspace.agent_prompt_pins',
        'workspace.agent_risk_tiers',
    ];

    protected function setUp(): void
    {
        parent::setUp();

        if (DB::connection()->getDriverName() !== 'pgsql') {
            $this->markTestSkipped('RLS and NULLS NOT DISTINCT are Postgres-only.');
        }
    }

    #[Test]
    public function every_ported_object_exists(): void
    {
        $missing = [];

        foreach (self::PORTED_TABLES as $qualified) {
            [$schema, $table] = explode('.', $qualified, 2);

            $exists = DB::selectOne(
                'SELECT 1 AS ok FROM information_schema.tables
                  WHERE table_schema = ? AND table_name = ?',
                [$schema, $table],
            );

            if ($exists === null) {
                $missing[] = $qualified;
            }
        }

        $fn = DB::selectOne(
            "SELECT 1 AS ok FROM pg_proc p
               JOIN pg_namespace n ON n.oid = p.pronamespace
              WHERE n.nspname = 'workspace' AND p.proname = 'feature_flags_audit'",
        );

        if ($fn === null) {
            $missing[] = 'function workspace.feature_flags_audit';
        }

        $this->assertSame(
            [],
            $missing,
            "Ported operational-core objects are missing. These are created by the\n"
            .'2026_08_28_1000xx–1003xx migrations; if one is absent the migration chain '
            ."did not run to completion.\nMissing:\n  ".implode("\n  ", $missing),
        );
    }

    #[Test]
    public function the_verified_subset_is_fail_closed(): void
    {
        $offenders = [];

        foreach (self::FAIL_CLOSED as $qualified) {
            [$schema, $table] = explode('.', $qualified, 2);

            $row = DB::selectOne(
                'SELECT qual, with_check FROM pg_policies
                  WHERE schemaname = ? AND tablename = ? AND policyname = ?',
                [$schema, $table, 'tenant_isolation'],
            );

            if ($row === null) {
                $offenders[] = "{$qualified}: no tenant_isolation policy at all";

                continue;
            }

            // The fail-open shape reads `<guc expr> IS NULL OR ...`. A closed
            // policy compares workspace_id to the GUC and nothing else, so any
            // IS NULL branch means the escape hatch is back.
            foreach (['qual' => $row->qual, 'with_check' => $row->with_check] as $clause => $sql) {
                if ($sql !== null && stripos($sql, 'IS NULL') !== false) {
                    $offenders[] = "{$qualified}: {$clause} carries a fail-open branch — {$sql}";
                }
            }
        }

        $this->assertSame(
            [],
            $offenders,
            "These tables are in the verified fail-closed subset of\n"
            ."2026_08_14_030000_close_rls_admin_escape_hatch_verified_subset. An unset\n"
            ."app.workspace_id must admit NO rows, not every row.\n  "
            .implode("\n  ", $offenders),
        );
    }

    #[Test]
    public function global_config_tables_are_deliberately_unscoped(): void
    {
        $offenders = [];

        foreach (self::GLOBAL_CONFIG as $qualified) {
            [$schema, $table] = explode('.', $qualified, 2);

            $hasWorkspaceId = DB::selectOne(
                'SELECT 1 AS ok FROM information_schema.columns
                  WHERE table_schema = ? AND table_name = ? AND column_name = ?',
                [$schema, $table, 'workspace_id'],
            );

            if ($hasWorkspaceId !== null) {
                $offenders[] = "{$qualified}: gained a workspace_id column — it now needs RLS "
                    .'and belongs in WorkspaceRlsCoverageTest, not here';
            }
        }

        $this->assertSame(
            [],
            $offenders,
            "phase0/95-rls-policies.sql lists these under \"Tables that DO NOT get RLS\n"
            ."... (global config)\". They carry no tenant data and nothing to scope on.\n  "
            .implode("\n  ", $offenders),
        );
    }

    #[Test]
    public function the_feature_flag_audit_trigger_records_changes_and_skips_no_ops(): void
    {
        $flag = 'tests.operational_core_parity.'.bin2hex(random_bytes(6));

        DB::beginTransaction();

        try {
            DB::statement("SET LOCAL app.actor_id = '4242'");

            DB::insert(
                'INSERT INTO workspace.feature_flags (workspace_id, flag_name, bool_value)
                 VALUES (NULL, ?, true)',
                [$flag],
            );
            DB::update(
                'UPDATE workspace.feature_flags SET bool_value = false WHERE flag_name = ?',
                [$flag],
            );
            // Description-only change: the trigger must NOT record this.
            DB::update(
                'UPDATE workspace.feature_flags SET description = ? WHERE flag_name = ?',
                ['touched', $flag],
            );

            $history = DB::select(
                'SELECT op, old_bool_value, new_bool_value, actor_id
                   FROM workspace.feature_flag_history
                  WHERE flag_name = ? ORDER BY changed_at',
                [$flag],
            );

            $this->assertCount(
                2,
                $history,
                'Expected exactly INSERT + UPDATE. A third row means the no-op '
                .'UPDATE guard in workspace.feature_flags_audit() stopped working; '
                .'zero rows means the trigger is not attached.',
            );

            $this->assertSame('INSERT', $history[0]->op);
            $this->assertSame('UPDATE', $history[1]->op);
            $this->assertSame(
                4242,
                (int) $history[1]->actor_id,
                'actor_id is read from the app.actor_id GUC at trigger fire time.',
            );
        } finally {
            DB::rollBack();
        }
    }
}
