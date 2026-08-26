<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * silver.storage_tier_policy: make platform defaults visible again, and stop
 * them multiplying.
 *
 * WHAT THE TABLE IS
 * -----------------
 * Its own DDL answers the tenancy question this migration acts on. The table
 * comment (database/raw/phase0/70-layer-g-findings.sql) reads "workspace_id
 * NULL = platform default", the seed inserts every row with workspace_id
 * NULL, and the ONLY production reader — the Storage Tiering Agent
 * (src/fastapi/app/agents/phase0/storage_tiering.py) — selects
 *
 *     WHERE is_active = true AND (workspace_id IS NULL OR workspace_id = $1)
 *
 * i.e. it was written expecting NULL rows to be global infrastructure config
 * layered under optional per-workspace overrides. This is the rock_codes
 * situation (2026_08_25_190000) with the OPPOSITE resolution: rock_codes is
 * workspace-extensible reference data, so it was replicated per tenant;
 * tiering rules are platform config, so replication would just let ten copies
 * drift apart. The policy adapts to the data, not the data to the policy.
 *
 * BUG 1 — the RLS policy hides every row from the application
 * -----------------------------------------------------------
 * Measured against live Azure 2026-08-25: the policy there is the strict
 * fail-closed shape
 *
 *     workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
 *
 * under which a NULL workspace_id matches nothing — 10 rows visible with the
 * GUC unset (owner probe), 0 with it bound to a real workspace. The agent has
 * been running against zero rules ("no active storage_tier_policy rows —
 * nothing to do"). The dev database has a different broken shape (the phase0
 * `tenant_isolation` macro with fail-open branches layered on top of it), so
 * this migration drops WHATEVER policies exist on the table rather than
 * assuming a name, then installs the canonical nullable-aware shape:
 *
 *     workspace_id IS NULL OR workspace_id = <scope>
 *
 * Same shape 2026_08_21_030000 deliberately preserved for the nullable
 * audit.audit_ledger_chain_fork_quarantine, and fail-closed where it matters:
 * an unbound GUC sees ONLY the platform defaults, never another tenant's
 * overrides. WITH CHECK intentionally defaults to the USING expression
 * (also matching that precedent): a session with write grants can maintain
 * platform rows — which is how owner sessions and migrations under FORCE ROW
 * LEVEL SECURITY manage the defaults at all. No application code writes this
 * table today; the tiering agent only reads it.
 *
 * BUG 2 — ON CONFLICT never fires for the seed, so re-applies duplicate it
 * ------------------------------------------------------------------------
 * The seed ends with ON CONFLICT (workspace_id, ...) DO NOTHING, but the
 * backing UNIQUE constraint treats NULLs as distinct (the Postgres default),
 * so NULL-workspace rows never conflict and every re-apply of the re-runnable
 * raw file inserts 5 more rows. Live Azure holds 10 rows = the 5-rule seed
 * applied twice; dev held 450 = ninety applies. Fixed by deduplicating
 * (keeping the earliest row per rule) and rebuilding the constraint as
 * UNIQUE NULLS NOT DISTINCT (PG 15+; this cluster is 18.3), which makes the
 * seed's ON CONFLICT actually fire from now on.
 *
 * ORDERING NOTE: the policy swap must run BEFORE the dedupe. On clusters
 * where the strict shape is live and the table is under FORCE ROW LEVEL
 * SECURITY (it is — phase0 95-rls-policies.sql), the owner cannot see the
 * NULL rows either, so a dedupe run first would silently delete nothing and
 * the constraint rebuild would then fail on the surviving duplicates.
 */
return new class extends Migration
{
    private const QUALIFIED = 'silver.storage_tier_policy';

    private const POLICY = 'silver_storage_tier_policy_workspace_isolation';

    /** The canonical fail-closed scope, matching 2026_08_21_030000. */
    private const SCOPE = "(NULLIF(current_setting('app.workspace_id', true), ''))::uuid";

    public function up(): void
    {
        if (! $this->applies()) {
            return;
        }

        $this->replaceAllPolicies('workspace_id IS NULL OR workspace_id = '.self::SCOPE);

        DB::statement(<<<'SQL'
            DELETE FROM silver.storage_tier_policy t
             USING silver.storage_tier_policy k
             WHERE t.workspace_id IS NULL
               AND k.workspace_id IS NULL
               AND k.object_class = t.object_class
               AND k.source_tier  = t.source_tier
               AND k.target_tier  = t.target_tier
               AND (k.created_at, k.id) < (t.created_at, t.id)
        SQL);

        DB::statement(
            'ALTER TABLE '.self::QUALIFIED
            .' DROP CONSTRAINT IF EXISTS storage_tier_policy_unique_per_scope',
        );
        DB::statement(
            'ALTER TABLE '.self::QUALIFIED
            .' ADD CONSTRAINT storage_tier_policy_unique_per_scope'
            .' UNIQUE NULLS NOT DISTINCT (workspace_id, object_class, source_tier, target_tier)',
        );
    }

    /**
     * Restores the strict fail-closed policy and the NULLS DISTINCT
     * constraint. The dedupe is deliberately NOT reversed — re-inflating
     * duplicate seed rows is the disease, not a prior state worth restoring.
     */
    public function down(): void
    {
        if (! $this->applies()) {
            return;
        }

        $this->replaceAllPolicies('workspace_id = '.self::SCOPE);

        DB::statement(
            'ALTER TABLE '.self::QUALIFIED
            .' DROP CONSTRAINT IF EXISTS storage_tier_policy_unique_per_scope',
        );
        DB::statement(
            'ALTER TABLE '.self::QUALIFIED
            .' ADD CONSTRAINT storage_tier_policy_unique_per_scope'
            .' UNIQUE (workspace_id, object_class, source_tier, target_tier)',
        );
    }

    /**
     * Dev and Azure carry differently-named, differently-shaped policies on
     * this table (the strict rewrite on Azure was applied out of band and its
     * name is not in version control), so drop everything present and create
     * the one canonical policy.
     */
    private function replaceAllPolicies(string $using): void
    {
        $existing = DB::select(
            "SELECT policyname FROM pg_policies
              WHERE schemaname = 'silver' AND tablename = 'storage_tier_policy'",
        );

        foreach ($existing as $row) {
            DB::statement(sprintf(
                'DROP POLICY IF EXISTS %s ON %s',
                '"'.str_replace('"', '""', $row->policyname).'"',
                self::QUALIFIED,
            ));
        }

        DB::statement(sprintf(
            'CREATE POLICY %s ON %s USING (%s)',
            self::POLICY,
            self::QUALIFIED,
            $using,
        ));
    }

    /**
     * Postgres-only, and only where the phase0 raw SQL that creates this
     * table has actually run — the migrate-only test database does not have
     * it (see scripts/raw-parity-baseline.txt). Same guard shape as
     * 2026_08_19_070000 for the sibling corpus_health_findings table.
     */
    private function applies(): bool
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return false;
        }

        return (bool) DB::selectOne(
            'SELECT to_regclass(?) IS NOT NULL AS present',
            [self::QUALIFIED],
        )?->present;
    }
};
