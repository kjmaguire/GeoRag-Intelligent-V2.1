<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Tests\TestCase;

/**
 * Locks in the tenancy invariant exposed by the Lakehouse audit
 * 2026-05-25: any application table that carries a workspace_id column
 * MUST have RLS enabled with at least one policy.
 *
 * This is the durable backstop for the recurring pattern (CC-01/CC-03
 * + reliability spec migrations all forgot ENABLE ROW LEVEL SECURITY).
 * If a future migration adds a workspace_id column without the matching
 * policy, this test wakes up.
 *
 * Skipped on SQLite — RLS is a Postgres feature.
 *
 * Excluded schemas:
 *   - public                — Laravel's own tables (users, jobs, etc.)
 *                             carry no workspace_id and are user-scoped.
 *   - laravel-managed       — sessions, password_resets, etc.
 *
 * One table in that `public` exclusion is load-bearing for tenancy rather
 * than incidental to it: `project_user`, the pivot hasProjectAccess(),
 * isProjectOwner() and BindWorkspaceRlsContext all resolve through. It is
 * covered by the same rationale — no workspace_id, already scoped to one
 * user by the belongsToMany — but it reads like a hole and has been written
 * up as one more than once. ProjectUserPivotRlsExemptionTest holds the
 * argument, including why the obvious policy would be an outage rather than
 * a fix, and pins the facts it rests on.
 *
 * Excluded tables fall into three categories:
 *   1. Self-referential (silver.workspaces — tenant policy would block
 *      the membership lookup it depends on).
 *   2. Partition children — PostgreSQL doesn't propagate policies down
 *      the partition tree, but queries that go through the parent (the
 *      normal access path) get pruned + policy-evaluated correctly.
 *      Filtered via pg_inherits.
 *   3. Test-DB parity casualties — production picks up RLS from
 *      database/raw/phase0/96-rls-tenant-* SQL files that aren't run by
 *      RefreshDatabase. Listed in EXEMPT_TEST_DB_ONLY_TABLES below
 *      with verification dates so the list ages out as the test-DB
 *      bootstrap improves.
 */
final class WorkspaceRlsCoverageTest extends TestCase
{
    use RefreshDatabase;

    /**
     * Permanent exemptions (verified safe in every environment).
     *
     * @var list<string>
     */
    private const EXEMPT_TABLES = [
        // The workspaces registry itself — RLS would block reading the
        // very rows used to evaluate workspace membership.
        'silver.workspaces',
        // Platform-wide tenant-isolation audit LOG — RLS intentionally NOT
        // enabled (see 2026_05_30_000000_create_silver_tenant_isolation_audit
        // docstring): workspace_id is nullable for system-wide sweeps and the
        // table is admin-Gate-scoped, same pattern as workflow.flow_jwt_keys.
        // Verified 2026-06-28: only RLS-off workspace_id table across ALL
        // tenant schemas in production.
        'silver.tenant_isolation_audit',
        // ops.* schema is GLOBAL by design — its own creating migration
        // (2026_05_13_140100_create_ops_support_schema) documents "no
        // workspace RLS; cross-workspace access is logged via
        // app.audit.emit_audit(action_type='support_access')" per §25.3.
        // Added 2026-08-17 after the CI-gap audit surfaced this table as a
        // false-positive gap — RLS here would contradict the documented
        // support-ops design, not fix a real leak.
        // NOTE (2026-10-10): production does not match that design.
        // database/raw/phase0/98-rls-tenant-isolation-block3.sql §3C puts
        // FORCE RLS and a strict tenant policy on this table after `migrate`,
        // as it does on the two support tables in EXEMPT_TEST_DB_ONLY_TABLES.
        // Whether ops.* is tenant-scoped or global is an open decision.
        'ops.support_tickets',
    ];

    /**
     * Tables whose RLS production gets from the raw layer, which
     * RefreshDatabase does not run. The 14 tables previously listed here
     * were reconciled into a proper Laravel migration on 2026-05-25
     * (2026_05_25_175214_enable_rls_on_phase0_workspace_tables_reconciliation),
     * which is a no-op against production (existing policies left
     * untouched) and a first-time install against the test DB.
     *
     * Every entry MUST include a follow-up note for how it will be
     * reconciled.
     *
     * @var list<string>
     */
    private const EXEMPT_TEST_DB_ONLY_TABLES = [
        // 2026-10-10. 2026_10_10_120000 adds workspace_id to both, nullable
        // and back-filled, so the support workflow can write it on a
        // migrate-only database as well. The policies stay in raw 98 (block3
        // §3A/§3B: NOT NULL, FK, FORCE RLS, strict tenant policy), applied
        // by `db:apply-raw` on every deploy. The "Cron sweeps under
        // georag_app" CI job checks that post-raw state
        // (test_support_replay_workspace_scope.py).
        // Reconcile: a migration mirroring block3 §3A-§3C for all three
        // support tables, as 2026_05_25_175214 did for 14 others. It retires
        // these two entries and the ops.support_tickets exemption, once the
        // tenancy decision above is made.
        'ops.support_replay_runs',
        'ops.support_ticket_traces',
    ];

    public function test_every_workspace_scoped_table_has_rls_with_a_policy(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('RLS is Postgres-only.');
        }

        // Exclude partition children — pg_inherits.inhrelid means the
        // table is a child of a partitioned table, and partition policies
        // live on the parent (Postgres prunes + evaluates from there).
        $rows = DB::select(<<<'SQL'
            SELECT n.nspname AS schema,
                   c.relname AS table,
                   c.relrowsecurity AS rls_on,
                   EXISTS (
                     SELECT 1 FROM pg_policies p
                     WHERE p.schemaname = n.nspname AND p.tablename = c.relname
                   ) AS has_policy
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            -- Audit 2026-06-28: cover EVERY tenant schema by EXCLUDING only the
            -- system / Laravel-managed ones, rather than an allowlist that
            -- silently omitted workspace.*, interpretation, ops, outbox,
            -- targeting, usage and workflow (all carry workspace_id tables).
            -- A new tenant schema is now covered automatically.
            WHERE n.nspname NOT IN (
                'pg_catalog', 'information_schema', 'public',
                'partman', 'pgivm', 'topology', 'backups'
            )
              AND c.relkind = 'r'
              AND NOT EXISTS (
                SELECT 1 FROM pg_inherits i WHERE i.inhrelid = c.oid
              )
              AND EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = n.nspname
                  AND table_name = c.relname
                  AND column_name = 'workspace_id'
              )
            ORDER BY 1, 2
        SQL);

        $allExempt = array_merge(self::EXEMPT_TABLES, self::EXEMPT_TEST_DB_ONLY_TABLES);
        $gaps = [];
        foreach ($rows as $r) {
            $qualified = $r->schema.'.'.$r->table;
            if (in_array($qualified, $allExempt, true)) {
                continue;
            }
            if (! $r->rls_on || ! $r->has_policy) {
                $gaps[] = sprintf(
                    '%s (rls_on=%s, has_policy=%s)',
                    $qualified,
                    $r->rls_on ? 'true' : 'false',
                    $r->has_policy ? 'true' : 'false',
                );
            }
        }

        $this->assertSame(
            [],
            $gaps,
            'Tables with workspace_id but no RLS+policy: '.PHP_EOL.implode(PHP_EOL, $gaps).
            PHP_EOL.PHP_EOL.
            'Fix by adding ENABLE ROW LEVEL SECURITY + a workspace_isolation policy '.
            'in a new migration. See 2026_05_25_173814_enable_rls_on_post_phase0_workspace_tables '.
            'for the canonical template, or add to EXEMPT_TABLES with a comment if exempt.',
        );
    }

    /**
     * SECURITY regression test for the 2026-05-25 broken-GUC sweep.
     *
     * The original WorkspaceRlsCoverageTest above checks that RLS is
     * enabled + a policy exists, but a policy that references the wrong
     * GUC name still counts as "present" — yet behaves fail-open
     * because the GUC is never set by any app codepath. We caught 12
     * such policies during the deferred-items pass (silver.document_
     * passages, silver.answer_runs, etc.) — all using `georag.workspace_id`
     * or `georag.project_id` instead of the canonical `app.workspace_id`.
     * Migration 2026_05_25_180924 replaced them with the canonical
     * shape; this test stops the pattern from regressing.
     */
    public function test_no_policy_references_legacy_georag_gucs(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('RLS is Postgres-only.');
        }

        $offenders = DB::select(<<<'SQL'
            SELECT schemaname, tablename, policyname
              FROM pg_policies
             WHERE qual LIKE '%georag.workspace_id%'
                OR qual LIKE '%georag.project_id%'
                OR with_check LIKE '%georag.workspace_id%'
                OR with_check LIKE '%georag.project_id%'
             ORDER BY schemaname, tablename, policyname
        SQL);

        $msg = 'Policies still reference the legacy `georag.*` GUC namespace, '
            .'which is functionally fail-open because the app sets `app.workspace_id`. '
            .'Replace with the canonical workspace_isolation shape — see '
            .'2026_05_25_180924_replace_broken_guc_rls_policies_with_canonical '
            .'for the template.';

        $list = array_map(
            fn ($r) => "  {$r->schemaname}.{$r->tablename} → {$r->policyname}",
            $offenders,
        );

        $this->assertSame([], $list, $msg);
    }

    /**
     * SECURITY regression test for the 2026-05-28 chr(0) sentinel bug.
     *
     * `silver.workspaces.workspaces_tenant_isolation` and
     * `silver.target_rationales.target_rationales_workspace_isolation`
     * used `NULLIF(current_setting('app.workspace_id', true), chr(0))`
     * as their "GUC unset" sentinel. `chr(0)` produces a TEXT containing
     * a U+0000 byte; PG18 rejects that (`null character not permitted`),
     * which causes the policy expression to fail CLOSED under psycopg2
     * even when it was meant to fail OPEN. Migration
     * 2026_05_29_190000_replace_broken_chr0_rls_policies replaced both
     * with the empty-string sentinel (`''`) — the same shape used by
     * the canonical workspace_isolation policies. This test stops the
     * chr(0) sentinel from regressing.
     */
    public function test_no_policy_uses_chr_zero_sentinel(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('RLS is Postgres-only.');
        }

        $offenders = DB::select(<<<'SQL'
            SELECT schemaname, tablename, policyname
              FROM pg_policies
             WHERE qual LIKE '%chr(0)%'
                OR with_check LIKE '%chr(0)%'
             ORDER BY schemaname, tablename, policyname
        SQL);

        $msg = 'RLS policies still use chr(0) as the "GUC unset" sentinel. '
            .'chr(0) produces a TEXT with a U+0000 byte which PG18 rejects with '
            .'`null character not permitted`, causing the policy to fail CLOSED '
            .'under psycopg2 even when it was meant to fail OPEN. Replace with '
            .'`NULLIF(current_setting(\'app.workspace_id\', true), \'\')` — see '
            .'2026_05_29_190000_replace_broken_chr0_rls_policies for the template.';

        $list = array_map(
            fn ($r) => "  {$r->schemaname}.{$r->tablename} → {$r->policyname}",
            $offenders,
        );

        $this->assertSame([], $list, $msg);
    }

    /**
     * SECURITY regression test for the 2026-08-14 fail-open→fail-closed
     * pass (DB audit + security audit finding: EVERY workspace-scoped RLS
     * policy admits all rows when `app.workspace_id` is unset instead of
     * denying them — see 2026_05_13_160000_retrofit_rls_admin_escape_hatch
     * and the database/raw/phase0/111-112 bulk sweep for how the
     * escape-hatch shape became pervasive).
     *
     * Migration 2026_08_14_030000_close_rls_admin_escape_hatch_verified_
     * subset converted a STATIC-ANALYSIS-VERIFIED subset of tables (no
     * live Laravel or FastAPI request-time reader found that depends on
     * the unset-GUC escape) to fail-closed. This test locks that subset
     * in place so a future migration can't silently reintroduce the
     * escape hatch on exactly these tables.
     *
     * This is intentionally NOT a blanket "no policy anywhere may have
     * IS NULL OR" assertion — the large majority of workspace-scoped
     * tables (silver.projects, collars, reports, samples, answer_runs,
     * evidence_items, document_passages, exports, audit.audit_ledger,
     * source_trust_scores, targeting.*, and more) are still intentionally
     * fail-open, documented in the migration's docblock and the
     * accompanying audit report, pending a live-DB-verified follow-up
     * pass. Asserting them here would be testing a known, tracked gap,
     * not a regression.
     */
    public function test_verified_subset_has_no_fail_open_escape_hatch(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('RLS is Postgres-only.');
        }

        // Mirrors the TABLES constant in
        // 2026_08_14_030000_close_rls_admin_escape_hatch_verified_subset.
        // Kept as a literal list (not re-parsed from the migration) so a
        // typo in either place is caught by drift rather than silently
        // agreeing with itself.
        $converted = [
            ['workspace', 'workspace_memberships', 'tenant_isolation'],
            ['workspace', 'workspace_agent_config', 'tenant_isolation'],
            ['workspace', 'dry_run_outputs', 'tenant_isolation'],
            ['outbox', 'pending_propagations', 'tenant_isolation'],
            ['outbox', 'propagation_attempts', 'tenant_isolation'],
            ['usage', 'usage_events', 'usage_events_tenant_isolation'],
            ['usage', 'workspace_cost_ceilings', 'workspace_cost_ceilings_tenant_isolation'],
            ['silver', 'kg_formation_aliases', 'kg_formation_aliases_workspace_isolation'],
            ['silver', 'kg_mineral_aliases', 'kg_mineral_aliases_workspace_isolation'],
            ['silver', 'kg_report_aliases', 'kg_report_aliases_workspace_isolation'],
            ['silver', 'kg_sample_aliases', 'kg_sample_aliases_workspace_isolation'],
            ['silver', 'collaboration_audit_log', 'collaboration_audit_log_workspace_isolation'],
            ['silver', 'collaboration_comments', 'collaboration_comments_workspace_isolation'],
            ['silver', 'agent_conversation_messages', 'agent_conversation_messages_workspace_isolation'],
            ['silver', 'agent_conversations', 'agent_conversations_workspace_isolation'],
            ['silver', 'pdf_coordinates', 'pdf_coordinates_workspace_isolation'],
            ['silver', 'pdf_layout_regions', 'pdf_layout_regions_workspace_isolation'],
            ['silver', 'pdf_ocr_results', 'pdf_ocr_results_workspace_isolation'],
            ['silver', 'pdf_table_cells', 'pdf_table_cells_workspace_isolation'],
            ['silver', 'pdf_text_blocks', 'pdf_text_blocks_workspace_isolation'],
            ['silver', 'pdf_vl_summaries', 'pdf_vl_summaries_workspace_isolation'],
            ['silver', 'review_audit_log', 'review_audit_log_workspace_isolation'],
            ['silver', 'assay_events', 'assay_events_workspace_isolation'],
            ['silver', 'ingest_extractions', 'tenant_isolation'],
            ['silver', 'ingest_layouts', 'tenant_isolation'],
            ['silver', 'ingest_ocr_results', 'tenant_isolation'],
            ['silver', 'collab_anchors', 'silver_collab_anchors_workspace_isolation'],
            ['silver', 'tier3_unlock_requests', 'silver_tier3_unlock_requests_workspace_isolation'],
        ];

        $gaps = [];
        foreach ($converted as [$schema, $table, $policy]) {
            $row = DB::selectOne(
                'SELECT qual, with_check FROM pg_policies '
                .'WHERE schemaname = ? AND tablename = ? AND policyname = ?',
                [$schema, $table, $policy],
            );

            if ($row === null) {
                // Table/policy absent in this environment (e.g. a few of
                // these are phase0-raw-SQL-only tables not provisioned in
                // the migrate-only test DB — see the migration's
                // tableExists() guard). Not a regression to flag here.
                continue;
            }

            $qualified = "{$schema}.{$table}";
            if (self::hasUnboundEscape($row->qual)) {
                $gaps[] = "{$qualified} → {$policy} (USING still has an IS NULL OR escape)";
            }
            if (self::hasUnboundEscape($row->with_check)) {
                $gaps[] = "{$qualified} → {$policy} (WITH CHECK still has an IS NULL OR escape)";
            }
        }

        $this->assertSame(
            [],
            $gaps,
            'Fail-closed RLS regressed on the verified subset: '.PHP_EOL.implode(PHP_EOL, $gaps).
            PHP_EOL.PHP_EOL.
            'See 2026_08_14_030000_close_rls_admin_escape_hatch_verified_subset for the '
            .'canonical fail-closed shape.',
        );
    }

    /**
     * SECURITY regression test for the 2026-08-15 SECOND pass on the
     * fail-open→fail-closed conversion (this one live-DB-verified, not
     * static-analysis-only like the first pass above).
     *
     * Migration 2026_08_15_020100_close_rls_admin_escape_hatch_second_pass
     * converted 15 more tables — decision-intelligence (decision_records
     * + 4 child tables), geological hypotheses (hypotheses +
     * hypothesis_evidence_links), silver.saved_map_views, and 7 of the 8
     * targeting.* tables — to fail-closed, each verified against a live
     * Postgres container for (a) an actual workspace_id column of the
     * right shape and (b) no live Laravel/FastAPI request-time reader
     * depending on the unset-GUC escape (see that migration's docblock
     * for the full per-table evidence, including the PublicApiController
     * ::targets() and RecordDecision.php fixes that shipped alongside it).
     *
     * Still intentionally fail-open and NOT asserted here: audit.
     * audit_ledger (a deliberately NULLABLE workspace_id for legitimate
     * platform-wide / cross-workspace rows — flipping it would silently
     * hide those rows, a product decision beyond closing the escape
     * hatch; targeting.target_backtests had the same shape until
     * 2026_09_29_190000 kept its NULL rows visible but closed the
     * unset-GUC branch — see TargetBacktestsRlsTest) and
     * silver.source_trust_scores (its one live reader is an admin
     * cross-workspace listing endpoint that never binds the GUC by
     * design). Also still open: everything from the first pass's "much
     * larger remaining set" not covered by either batch (silver.projects,
     * collars, reports, samples, answer_runs, evidence_items, exports,
     * and more). silver.document_passages — the one exception — was
     * converted in the third pass; see
     * test_third_pass_document_passages_has_no_fail_open_escape_hatch
     * below.
     */
    public function test_second_verified_subset_has_no_fail_open_escape_hatch(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('RLS is Postgres-only.');
        }

        // Mirrors the TABLES constant in
        // 2026_08_15_020100_close_rls_admin_escape_hatch_second_pass.
        $converted = [
            ['silver', 'hypotheses', 'hypotheses_workspace_isolation'],
            ['silver', 'hypothesis_evidence_links', 'hypothesis_evidence_links_workspace_isolation'],
            ['silver', 'decision_records', 'decision_records_workspace_isolation'],
            ['silver', 'decision_evidence_links', 'decision_evidence_links_workspace_isolation'],
            ['silver', 'decision_options', 'decision_options_workspace_isolation'],
            ['silver', 'decision_outcomes', 'decision_outcomes_workspace_isolation'],
            ['silver', 'decision_lessons_learned', 'decision_lessons_learned_workspace_isolation'],
            ['silver', 'saved_map_views', 'saved_map_views_workspace_isolation'],
            ['targeting', 'target_recommendations', 'target_recommendations_workspace_isolation'],
            ['targeting', 'target_candidate_zones', 'target_candidate_zones_workspace_isolation'],
            ['targeting', 'target_scores', 'target_scores_workspace_isolation'],
            ['targeting', 'target_score_factors', 'target_score_factors_workspace_isolation'],
            ['targeting', 'target_uncertainties', 'target_uncertainties_workspace_isolation'],
            ['targeting', 'target_review_decisions', 'target_review_decisions_workspace_isolation'],
            ['targeting', 'target_outcomes', 'target_outcomes_workspace_isolation'],
        ];

        $gaps = [];
        foreach ($converted as [$schema, $table, $policy]) {
            $row = DB::selectOne(
                'SELECT qual, with_check FROM pg_policies '
                .'WHERE schemaname = ? AND tablename = ? AND policyname = ?',
                [$schema, $table, $policy],
            );

            if ($row === null) {
                // Table/policy absent in this environment — not a
                // regression to flag here.
                continue;
            }

            $qualified = "{$schema}.{$table}";
            if (self::hasUnboundEscape($row->qual)) {
                $gaps[] = "{$qualified} → {$policy} (USING still has an IS NULL OR escape)";
            }
            if (self::hasUnboundEscape($row->with_check)) {
                $gaps[] = "{$qualified} → {$policy} (WITH CHECK still has an IS NULL OR escape)";
            }
        }

        $this->assertSame(
            [],
            $gaps,
            'Fail-closed RLS regressed on the second verified subset: '.PHP_EOL.implode(PHP_EOL, $gaps).
            PHP_EOL.PHP_EOL.
            'See 2026_08_15_020100_close_rls_admin_escape_hatch_second_pass for the '
            .'canonical fail-closed shape.',
        );
    }

    /**
     * SECURITY regression test for the 2026-08-24 FORCE sweep.
     *
     * `ENABLE ROW LEVEL SECURITY` exempts the table's OWNER; only `FORCE`
     * makes the policy apply to it. A live census on 2026-08-24 found 75 of
     * 160 RLS-enabled production tables were still ENABLE-only, so the
     * `georag` owner role (what MIGRATE_DB_USERNAME connects as) bypassed
     * their policies entirely. Migration
     * 2026_08_24_010000_force_row_level_security_on_all_rls_enabled_tables
     * swept every such table via pg_class; this pins the invariant so a
     * future migration cannot reintroduce an owner-exempt table by writing
     * ENABLE without FORCE.
     */
    public function test_every_rls_enabled_table_is_forced(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('RLS is Postgres-only.');
        }

        $rows = DB::select(<<<'SQL'
            SELECT n.nspname || '.' || c.relname AS qualified
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE c.relkind IN ('r', 'p')
               AND c.relrowsecurity
               AND NOT c.relforcerowsecurity
               AND n.nspname NOT IN ('pg_catalog', 'information_schema')
             ORDER BY 1
        SQL);

        $this->assertSame(
            [],
            array_map(static fn ($r): string => $r->qualified, $rows),
            'Tables with RLS ENABLEd but not FORCEd — the owner role bypasses '
            .'their policies entirely. Add FORCE ROW LEVEL SECURITY alongside '
            .'ENABLE in the creating migration; see '
            .'2026_08_24_010000_force_row_level_security_on_all_rls_enabled_tables.',
        );
    }

    /**
     * SECURITY regression test for the 2026-08-15 THIRD pass on the
     * fail-open→fail-closed conversion — the first of the two prior
     * passes' "8 remaining high-traffic tables" to actually convert.
     *
     * Migration 2026_08_15_030000_close_rls_admin_escape_hatch_third_pass
     * converted silver.document_passages only, after a live-DB-verified
     * call-site audit found its one real gap surface (4 Foundry
     * controllers) was mechanically fixable with the established
     * withWorkspaceRls() pattern — see that migration's docblock for the
     * full per-table evidence on why the other 7 of the 8 (projects,
     * collars, reports, answer_runs, evidence_items, exports, samples)
     * stayed fail-open this pass (each has either a structural
     * opaque-ID-into-itself chicken-and-egg blocker, an unbound live
     * INSERT path, or a broad multi-file gap surface too large to fix
     * safely alongside this migration).
     */
    public function test_third_pass_document_passages_has_no_fail_open_escape_hatch(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('RLS is Postgres-only.');
        }

        $row = DB::selectOne(
            'SELECT qual, with_check FROM pg_policies '
            .'WHERE schemaname = ? AND tablename = ? AND policyname = ?',
            ['silver', 'document_passages', 'document_passages_workspace_isolation'],
        );

        if ($row === null) {
            // Table/policy absent in this environment — not a regression
            // to flag here (mirrors the tableExists()/columnExists() guard
            // in the migration itself).
            return;
        }

        $gaps = [];
        if (self::hasUnboundEscape($row->qual)) {
            $gaps[] = 'silver.document_passages → document_passages_workspace_isolation (USING still has an IS NULL OR escape)';
        }
        if (self::hasUnboundEscape($row->with_check)) {
            $gaps[] = 'silver.document_passages → document_passages_workspace_isolation (WITH CHECK still has an IS NULL OR escape)';
        }

        $this->assertSame(
            [],
            $gaps,
            'Fail-closed RLS regressed on silver.document_passages: '.PHP_EOL.implode(PHP_EOL, $gaps).
            PHP_EOL.PHP_EOL.
            'See 2026_08_15_030000_close_rls_admin_escape_hatch_third_pass for the '
            .'canonical fail-closed shape.',
        );
    }

    /**
     * Does a policy expression, as pg_policies deparses it, contain an
     * `IS NULL` test?
     *
     * Every fail-open shape in this schema has one: `GUC IS NULL OR ...`,
     * `... OR current_setting(...) IS NULL`, `workspace_id IS NULL OR ...`.
     * The detector used to be `str_contains($expr, 'IS NULL OR')`, which can
     * never match: PostgreSQL deparses each OR operand in parentheses, so the
     * text is `(... IS NULL) OR (...)` and the needle `IS NULL OR` does not
     * occur. Measured on a migrate + db:apply-raw database: 101 of 176 policies
     * contain an `IS NULL`, and none contains `IS NULL OR`. That made the
     * "verified subset" tests below pass whatever the policies said --
     * including with the fail-open shape db:apply-raw had re-installed on five
     * of those tables. The census in docs/architecture/fail-open-rls-posture-
     * 2026-08-21.md reached the same conclusion and recommends this form.
     *
     * Shape-independent on purpose: `IS NOT DISTINCT FROM` (the platform-aware
     * shape on outbox.*) is not an `IS NULL` test and does not match.
     */
    private static function hasUnboundEscape(mixed $expression): bool
    {
        return is_string($expression) && preg_match('/\bIS\s+NULL\b/i', $expression) === 1;
    }

    /**
     * The fail-open-by-layering half of the same bug: two PERMISSIVE policies
     * on one table are OR-ed, so a strict policy next to a fail-open one is
     * fail-open. On a migrate-only database every verified-subset table must
     * carry exactly the one policy the migration chain gives it.
     *
     * The database/raw layer, which is where a second policy used to come from,
     * is not applied here (RefreshDatabase migrates only); the same assertion
     * against a migrate-THEN-raw database lives in
     * src/fastapi/tests/test_rls_after_raw.py, run by the CI job
     * cron-sweeps-app-role.
     */
    public function test_verified_subset_tables_carry_exactly_one_policy_each(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('RLS is Postgres-only.');
        }

        $tables = [
            ['workspace', 'workspace_memberships'],
            ['workspace', 'workspace_agent_config'],
            ['workspace', 'dry_run_outputs'],
            ['outbox', 'pending_propagations'],
            ['outbox', 'propagation_attempts'],
            ['usage', 'usage_events'],
            ['usage', 'workspace_cost_ceilings'],
        ];

        $layered = [];
        foreach ($tables as [$schema, $table]) {
            $names = array_map(
                static fn ($r) => $r->policyname,
                DB::select(
                    'SELECT policyname FROM pg_policies WHERE schemaname = ? AND tablename = ? ORDER BY policyname',
                    [$schema, $table],
                ),
            );

            if (count($names) > 1) {
                $layered[] = "{$schema}.{$table}: ".implode(', ', $names);
            }
        }

        $this->assertSame(
            [],
            $layered,
            'These tables carry more than one policy, and permissive policies OR together: '.PHP_EOL.implode(PHP_EOL, $layered),
        );
    }
}
