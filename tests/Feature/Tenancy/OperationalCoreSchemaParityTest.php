<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Illuminate\Support\Facades\DB;
use PHPUnit\Framework\Attributes\Test;
use Tests\TestCase;

/**
 * Pins the twenty-four objects ported out of `database/raw/` and into the
 * migration chain on 2026-08-28 — the operational core (outbox, the Layer-E
 * workspace tables, the tool gateway, feature flags), the §19.3 interpretation
 * schema, the Layer-G silver findings tables, and the §7.4 claim ledger plus
 * Phase-H4 workspace settings.
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
 * 4. Every workspace-scoped interpretation table has an index covering
 *    workspace_id. `interpretation_comments` did not in the raw DDL, so every
 *    evaluation of its RLS policy would have been a sequential scan.
 * 5. The `feature_flags_audit` trigger fires and skips no-op updates.
 * 6. `silver.store_reconciliation_findings.drift_type` admits
 *    `cross_store_drift`. The raw DDL's CHECK does not, while
 *    `agents/phase0/store_reconciliation.py` writes exactly that value inside
 *    an `except Exception` — so the constraint silently discarded the finding
 *    rather than failing loudly. Trimming the value back out of the CHECK
 *    would restore that silent loss, and nothing else would notice.
 * 7. `silver.corpus_health_findings.workspace_id` is NULLABLE. The raw DDL
 *    says NOT NULL and `2026_08_19_070000` drops it, but that migration is
 *    guarded on the table existing and has already run and skipped — so it
 *    will never fire again. This port is the only thing carrying its intent,
 *    and the only thing that will catch a regression.
 * 8. `silver.workspace_settings` is FAIL-CLOSED on both arms.
 *    `src/fastapi/tests/test_workspace_settings_rls_integration.py` pins the
 *    same contract, but it is an integration test against a live cluster and
 *    does not run in this suite — so a port that quietly adopted the
 *    fail-open house shape would sail through PR CI. Asserted here too.
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
    /** Every object the 2026_08_28_1000xx–1006xx migrations create. */
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
        'interpretation.interpretation_notes',
        'interpretation.interpretation_section_lines',
        'interpretation.interpretation_target_zones',
        'interpretation.interpretation_comments',
        'silver.store_reconciliation_findings',
        'silver.corpus_health_findings',
        'silver.storage_tier_policy',
        'silver.claim_ledger',
        'silver.workspace_settings',
    ];

    /**
     * Tables whose RLS policy filters on workspace_id and which therefore need
     * an index covering it — without one, every policy evaluation is a
     * sequential scan. interpretation_comments had exactly that gap in the raw
     * DDL and is the reason this check exists.
     *
     * Note the §11.5 index gate in routers/audit_findings.py does NOT cover
     * these: it scans silver/gold/audit/ops/workflow/targeting only, and
     * `interpretation` is not in that set. Nothing else asserts this, which is
     * precisely why it is asserted here.
     */
    private const NEEDS_WORKSPACE_INDEX = [
        'interpretation.interpretation_notes',
        'interpretation.interpretation_section_lines',
        'interpretation.interpretation_target_zones',
        'interpretation.interpretation_comments',
        // The §11.5 index gate DOES reach these three (silver is in its
        // _TENANT_SCHEMAS and none is exempt), so a missing index here would
        // surface as a live tenant-isolation finding as well as a seq scan.
        'silver.store_reconciliation_findings',
        'silver.corpus_health_findings',
        'silver.storage_tier_policy',
        'silver.claim_ledger',
        'silver.workspace_settings',
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
            .'2026_08_28_1000xx–1006xx migrations; if one is absent the migration chain '
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
    public function workspace_scoped_tables_have_a_covering_index(): void
    {
        $missing = [];

        foreach (self::NEEDS_WORKSPACE_INDEX as $qualified) {
            [$schema, $table] = explode('.', $qualified, 2);

            $row = DB::selectOne(
                "SELECT count(*) AS n FROM pg_indexes
                  WHERE schemaname = ? AND tablename = ?
                    AND indexdef ILIKE '%workspace_id%'",
                [$schema, $table],
            );

            if ((int) ($row->n ?? 0) === 0) {
                $missing[] = $qualified;
            }
        }

        $this->assertSame(
            [],
            $missing,
            "Every read of these tables is filtered by workspace_id through their\n"
            ."RLS policy, so without a covering index each policy evaluation is a\n"
            ."sequential scan — and the Tenant Isolation Auditor fails the table.\n  "
            .implode("\n  ", $missing),
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

    /**
     * The Store Reconciliation Agent's cross-store comparison writes
     * `drift_type = 'cross_store_drift'`, which the raw DDL's five-value CHECK
     * rejects — and the INSERT sits inside `except Exception`, so the finding
     * is dropped with a log line instead of raising. Asserted behaviourally
     * rather than by reading pg_constraint: what matters is that the row lands.
     */
    #[Test]
    public function store_reconciliation_findings_accepts_the_agents_cross_store_drift_type(): void
    {
        DB::beginTransaction();

        try {
            $workspaceId = DB::selectOne(
                'SELECT workspace_id FROM silver.workspaces LIMIT 1',
            )?->workspace_id;

            if ($workspaceId === null) {
                $workspaceId = '5c2d0a11-0000-4000-8000-0000000005c2';
                DB::insert(
                    'INSERT INTO silver.workspaces (workspace_id, name, slug)
                     VALUES (?::uuid, ?, ?)',
                    [$workspaceId, 'Drift probe', 'zz-drift-probe'],
                );
            }

            $written = DB::affectingStatement(
                <<<'SQL'
                INSERT INTO silver.store_reconciliation_findings
                    (workspace_id, drift_type, severity, source_store, target_store,
                     source_id, details, discovered_by)
                VALUES (?::uuid, 'cross_store_drift', 'high', 'postgres', 'qdrant_georag_chunks',
                        'qdrant_georag_chunks', '{}'::jsonb, 'Store Reconciliation Agent')
                SQL,
                [$workspaceId],
            );

            $this->assertSame(
                1,
                $written,
                "store_reconciliation.py writes drift_type='cross_store_drift' whenever the\n"
                ."Postgres and Qdrant passage counts diverge past its threshold. If the CHECK\n"
                ."rejects it, that INSERT raises inside `except Exception` and the finding is\n"
                .'lost with nothing but a warning in the logs.',
            );
        } finally {
            DB::rollBack();
        }
    }

    /**
     * 2026_08_19_070000 dropped this NOT NULL so the index-health agent could
     * persist its cluster-scoped probes (pg_stat_statements et al. have no
     * tenant). That migration is guarded on the table existing and has already
     * run and skipped, so it will never fire again — this port is the only
     * thing carrying its intent forward.
     */
    #[Test]
    public function corpus_health_findings_accepts_a_system_scoped_row(): void
    {
        $isNullable = DB::selectOne(
            "SELECT is_nullable FROM information_schema.columns
              WHERE table_schema = 'silver' AND table_name = 'corpus_health_findings'
                AND column_name = 'workspace_id'",
        )?->is_nullable;

        $this->assertSame(
            'YES',
            $isNullable,
            'silver.corpus_health_findings.workspace_id is NOT NULL again — every '
            .'system-wide run of index_health.py will fail to persist a single finding, '
            .'silently, exactly as it did before 2026_08_19_070000.',
        );

        DB::beginTransaction();

        try {
            $written = DB::affectingStatement(
                <<<'SQL'
                INSERT INTO silver.corpus_health_findings
                    (workspace_id, finding_type, severity, target_id, payload, status)
                VALUES (NULL, 'slow_query', 'medium', '4242', '{}'::jsonb, 'open')
                SQL,
            );

            $this->assertSame(1, $written, 'A system-scoped finding must be insertable.');
        } finally {
            DB::rollBack();
        }
    }

    /**
     * silver.workspace_settings must stay fail-closed on BOTH arms: an unset
     * GUC sees nothing, and a write naming another workspace is refused.
     *
     * The same contract is pinned by
     * src/fastapi/tests/test_workspace_settings_rls_integration.py, but that
     * is an integration test against a live cluster and is not part of the
     * PR-time suite. Without this, a future edit relaxing the policy to the
     * fail-open shape used elsewhere in silver would pass CI — while silently
     * making every workspace's allow_external_llm egress flag readable by any
     * unbound session.
     */
    #[Test]
    public function workspace_settings_stays_fail_closed_on_both_arms(): void
    {
        $row = DB::selectOne(
            'SELECT qual, with_check FROM pg_policies
              WHERE schemaname = ? AND tablename = ? AND policyname = ?',
            ['silver', 'workspace_settings', 'workspace_settings_workspace_isolation'],
        );

        $this->assertNotNull(
            $row,
            'Policy workspace_settings_workspace_isolation is gone. The table carries the '
            .'allow_external_llm egress flag read by app/agent/egress_gate.py.',
        );

        foreach (['qual' => $row->qual, 'with_check' => $row->with_check] as $clause => $sql) {
            $this->assertNotNull($sql, "{$clause} must be present, not defaulted.");
            $this->assertStringNotContainsString(
                'IS NULL',
                (string) $sql,
                "workspace_settings {$clause} gained a fail-open branch: {$sql}",
            );
        }
    }

    /**
     * silver.claim_ledger is deliberately MIXED — fail-open read, strict
     * write. Both halves are asserted because each could regress on its own:
     * widening WITH CHECK would let an unbound writer attribute claims to any
     * workspace, and every call site in services/claim_ledger.py binds the
     * scope first precisely because that arm is strict.
     */
    #[Test]
    public function claim_ledger_keeps_its_strict_write_arm(): void
    {
        $row = DB::selectOne(
            'SELECT qual, with_check FROM pg_policies
              WHERE schemaname = ? AND tablename = ? AND policyname = ?',
            ['silver', 'claim_ledger', 'claim_ledger_ws_isolation'],
        );

        $this->assertNotNull($row, 'Policy claim_ledger_ws_isolation is gone.');

        $this->assertStringNotContainsString(
            'IS NULL',
            (string) $row->with_check,
            'claim_ledger WITH CHECK gained a fail-open branch, so a session with no '
            .'app.workspace_id could now write a claim attributed to any workspace: '
            .$row->with_check,
        );

        // The read arm IS fail-open today, matching the raw file and the rest
        // of silver. Asserted so that tightening it is a deliberate edit here
        // rather than an unnoticed behaviour change — see
        // docs/architecture/fail-open-rls-posture-2026-08-21.md.
        $this->assertStringContainsString(
            'IS NULL',
            (string) $row->qual,
            'claim_ledger USING is no longer fail-open. That is very likely an '
            .'improvement, but it changes what an unbound reader sees — update this '
            .'assertion deliberately rather than deleting it.',
        );
    }
}
