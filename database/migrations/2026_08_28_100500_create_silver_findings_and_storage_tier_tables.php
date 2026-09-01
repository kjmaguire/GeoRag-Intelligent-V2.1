<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Port the Layer-G findings tables into the migration chain.
 *
 *   silver.store_reconciliation_findings  per-row drift between Postgres and a secondary store
 *   silver.corpus_health_findings         content-level findings (index-health + corpus-health agents)
 *   silver.storage_tier_policy            per-workspace × object-class tier transition rules
 *
 * Declared only in `database/raw/phase0/70-layer-g-findings.sql`, which CD
 * never runs — three consecutive entries in `scripts/raw-parity-baseline.txt`.
 *
 * ## Why these are not latent
 *
 * All three have live readers and writers today, so on Azure each is a
 * `42P01 undefined_table` rather than a dormant capability:
 *
 *   - `agents/phase0/store_reconciliation.py` INSERTs findings on every run of
 *     the Store Reconciliation Agent.
 *   - `agents/phase0/index_health.py` writes system-scoped rows into
 *     `corpus_health_findings` — and its `except Exception` per probe means
 *     the missing table has been swallowed silently.
 *   - `agents/phase0/storage_tiering.py` SELECTs `storage_tier_policy` with
 *     `WHERE is_active = true AND (workspace_id IS NULL OR workspace_id = $1)`
 *     and logs "nothing to do" when it comes back empty — which is
 *     indistinguishable from the table not existing at all.
 *
 * ## Three reconciliations the raw file does not carry
 *
 * Porting `70-layer-g-findings.sql` verbatim would produce the WRONG schema,
 * because two later migrations already ran against these tables and skipped —
 * each is guarded on the table existing, so neither will fire again once this
 * migration creates them. Their intent has to be folded in here instead.
 *
 * **1. `corpus_health_findings.workspace_id` is NULLABLE.** The raw DDL says
 * `NOT NULL`; `2026_08_19_070000_allow_system_scoped_corpus_health_findings`
 * drops that, because every probe in `index_health.py` reads a CLUSTER-scoped
 * catalog (`pg_stat_statements`, `pg_stat_user_tables`,
 * `pg_stat_user_indexes`) and the cron trigger deliberately passes no
 * workspace. Creating the column `NOT NULL` here would silently undo that
 * decision — the guarded migration has long since been marked as run. The FK
 * to `silver.workspaces` is kept: NULL satisfies a foreign key, so a
 * system-scoped row stays structurally valid while a row that DOES name a
 * workspace is still forced to name a real one.
 *
 * **2. `storage_tier_policy` gets one policy, and it is not the macro's.**
 * The raw file's own `95-rls-policies.sql` excludes this table from the
 * `tenant_isolation` macro and installs a dedicated
 * `silver_storage_tier_policy_workspace_isolation` instead, matching
 * `2026_08_25_200000_restore_platform_default_visibility_on_storage_tier_policy`.
 * `workspace_id NULL` here means PLATFORM DEFAULT, and the macro's
 * `IS NOT DISTINCT FROM` shape hides those rows from any workspace-bound
 * session — measured at 0 rows on live Azure, which is why the tiering agent
 * had been running against an empty rule set. The canonical shape exempts on
 * the COLUMN (`workspace_id IS NULL OR workspace_id = <scope>`) and stays
 * fail-closed for tenant rows: an unbound GUC sees the defaults and never
 * another tenant's overrides. `StorageTierPolicyPlatformDefaultsTest` asserts
 * exactly one policy by that name, with `workspace_id IS NULL` present and no
 * `current_setting(...) IS NULL` fail-open branch; it has been skipping on
 * "table absent" and starts running the moment this migration lands.
 *
 * **3. The unique constraint must be `NULLS NOT DISTINCT`.** With the
 * Postgres default, NULL-workspace rows never conflict, the seed's
 * `ON CONFLICT DO NOTHING` never fires, and every re-apply of the re-runnable
 * raw file inserts five more copies (live Azure reached 10, dev 450). The raw
 * file has since been corrected in place, so it is ported as written — but it
 * is the load-bearing half of that migration's fix and is called out rather
 * than left to look incidental.
 *
 * **4. `drift_type` admits `cross_store_drift`, which the raw CHECK rejects.**
 * `agents/phase0/store_reconciliation.py` compares the Postgres passage count
 * against Qdrant's and, when the two diverge by more than 10 rows AND 5%,
 * INSERTs a finding with `drift_type = 'cross_store_drift'` — a value absent
 * from the raw file's five-value taxonomy. That insert is wrapped in
 * `except Exception: logger.warning(...)`, so on the dev clusters where
 * `db:apply-raw` HAS created this table, every cross-store drift finding has
 * been rejected by the CHECK and dropped with only a log line. Porting the
 * constraint verbatim would ship that silent data loss to production on the
 * table's first day. The value is added to the CHECK: it is a deliberate,
 * named drift category with its own `summary["cross_store_drift"]` key, not a
 * typo, and the constraint exists to catch typos. (The Neo4j half of that
 * comparison is permanently `None` post-2026-07-28, so only the Qdrant
 * comparison can actually fire.)
 *
 * `2026_05_30_000000_create_silver_tenant_isolation_audit` needs no
 * reconciliation despite naming `store_reconciliation_findings` — it does so
 * only in a comment, while creating a different table.
 *
 * ## RLS
 *
 * The two findings tables get the phase0 `tenant_isolation` macro shape
 * verbatim — fail-open on an unset GUC, NULL-safe via `IS NOT DISTINCT FROM`.
 * Neither is in a verified fail-closed subset, so tightening them belongs with
 * the tiered work in `docs/architecture/fail-open-rls-posture-2026-08-21.md`.
 * The NULL-safe arm is what lets `index_health.py` write its system-scoped
 * rows at all, so it is load-bearing on `corpus_health_findings` specifically.
 *
 * `FORCE ROW LEVEL SECURITY` is added alongside every `ENABLE`. The raw macro
 * already does this, but it is worth stating why it cannot be dropped: the
 * catalog sweep in `2026_08_24_010000_force_row_level_security_on_all_rls_
 * enabled_tables` is one-shot and already ran, so a table created afterwards
 * with `ENABLE` alone leaves the `georag` owner bypassing its own policy —
 * which `WorkspaceRlsCoverageTest::test_every_rls_enabled_table_is_forced`
 * asserts against.
 *
 * Every table carries a leading-`workspace_id` index, so RLS policy evaluation
 * is an index scan rather than a sequential one. That also satisfies the
 * `gate="index"` check in `routers/audit_findings.py::get_tenant_isolation_
 * findings`, which flags any table in the `silver`/`gold`/`audit`/`ops`/
 * `workflow`/`targeting` schemas that has a `workspace_id` column and no index
 * mentioning it. All three of these are in `silver` and none is on its exempt
 * list, so a missing index would have shown up as a live §11.5 finding.
 *
 * Idempotent: `IF NOT EXISTS` throughout, `DROP POLICY IF EXISTS` before each
 * `CREATE POLICY`, `ON CONFLICT DO NOTHING` on the seed.
 */
return new class extends Migration
{
    /** Tables taking the phase0 `tenant_isolation` macro policy verbatim. */
    private const MACRO_RLS_TABLES = [
        'silver.store_reconciliation_findings',
        'silver.corpus_health_findings',
    ];

    private const TIER_POLICY_TABLE = 'silver.storage_tier_policy';

    private const TIER_POLICY_NAME = 'silver_storage_tier_policy_workspace_isolation';

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // ── silver.store_reconciliation_findings ──────────────────────────
        // drift_type carries the kickoff taxonomy PLUS 'cross_store_drift' —
        // see the class docblock. Five of the six have live writers:
        // store_reconciliation.py emits missing_in_b, stuck_propagation,
        // outbox_dead_letter and cross_store_drift; tenant_isolation_auditor.py
        // emits orphan_in_b on a cross-workspace leak. Only hash_mismatch is
        // unwritten today, and it is kept so its arrival needs no schema change.
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.store_reconciliation_findings (
                id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id     uuid        NOT NULL
                    REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                drift_type       text        NOT NULL,
                severity         text        NOT NULL DEFAULT 'medium',
                source_store     text        NOT NULL DEFAULT 'postgres',
                target_store     text        NOT NULL,
                source_id        text        NULL,
                target_id        text        NULL,
                details          jsonb       NOT NULL DEFAULT '{}'::jsonb,
                status           text        NOT NULL DEFAULT 'open',
                discovered_by    text        NULL,
                discovered_at    timestamptz NOT NULL DEFAULT now(),
                resolved_at      timestamptz NULL,
                resolved_by      bigint      NULL,
                resolution_notes text        NULL,
                CONSTRAINT store_reconciliation_findings_drift_type_check CHECK (
                    drift_type IN (
                        'missing_in_b','orphan_in_b','hash_mismatch',
                        'stuck_propagation','outbox_dead_letter',
                        'cross_store_drift'
                    )
                ),
                CONSTRAINT store_reconciliation_findings_severity_check CHECK (
                    severity IN ('critical','high','medium','low','info')
                ),
                CONSTRAINT store_reconciliation_findings_status_check CHECK (
                    status IN ('open','investigating','resolved','wontfix')
                )
            )
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON TABLE silver.store_reconciliation_findings IS
                'Per-row drift findings from Store Reconciliation Agent (Phase 0 agent #5).'
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS store_reconciliation_findings_workspace_idx
                ON silver.store_reconciliation_findings (workspace_id, status, discovered_at DESC)
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS store_reconciliation_findings_open_severity_idx
                ON silver.store_reconciliation_findings (severity, discovered_at DESC)
             WHERE status = 'open'
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS store_reconciliation_findings_drift_type_idx
                ON silver.store_reconciliation_findings (drift_type, target_store, discovered_at DESC)
        SQL);

        // ── silver.corpus_health_findings ─────────────────────────────────
        // workspace_id NULLABLE — see the class docblock. finding_type is
        // deliberately unconstrained text: index_health.py emits its own
        // vocabulary ('slow_query', 'table_bloat', 'zero_hit_index') alongside
        // whatever the Phase 3 corpus-health agent will add, and a CHECK here
        // would force a migration per probe.
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.corpus_health_findings (
                id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id  uuid        NULL
                    REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                finding_type  text        NOT NULL,
                severity      text        NOT NULL DEFAULT 'medium',
                target_schema text        NULL,
                target_table  text        NULL,
                target_id     text        NULL,
                payload       jsonb       NOT NULL DEFAULT '{}'::jsonb,
                status        text        NOT NULL DEFAULT 'open',
                discovered_at timestamptz NOT NULL DEFAULT now(),
                resolved_at   timestamptz NULL,
                resolved_by   bigint      NULL,
                CONSTRAINT corpus_health_findings_severity_check CHECK (
                    severity IN ('critical','high','medium','low','info')
                ),
                CONSTRAINT corpus_health_findings_status_check CHECK (
                    status IN ('open','investigating','resolved','wontfix')
                )
            )
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON TABLE silver.corpus_health_findings IS
                'Content-level corpus findings. workspace_id NULL = system-scoped (cluster-wide probe).'
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS corpus_health_findings_workspace_idx
                ON silver.corpus_health_findings (workspace_id, status, discovered_at DESC)
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS corpus_health_findings_type_idx
                ON silver.corpus_health_findings (finding_type, discovered_at DESC)
        SQL);

        // ── silver.storage_tier_policy ────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.storage_tier_policy (
                id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id       uuid        NULL
                    REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                object_class       text        NOT NULL,
                source_tier        text        NOT NULL,
                target_tier        text        NOT NULL,
                age_threshold_days integer     NOT NULL,
                is_active          boolean     NOT NULL DEFAULT true,
                priority           smallint    NOT NULL DEFAULT 100,
                created_at         timestamptz NOT NULL DEFAULT now(),
                updated_at         timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT storage_tier_policy_source_tier_check
                    CHECK (source_tier IN ('hot','warm','cold')),
                CONSTRAINT storage_tier_policy_target_tier_check
                    CHECK (target_tier IN ('hot','warm','cold')),
                CONSTRAINT storage_tier_policy_age_threshold_days_check
                    CHECK (age_threshold_days > 0),
                CONSTRAINT storage_tier_policy_source_target_distinct
                    CHECK (source_tier <> target_tier),
                -- NULLS NOT DISTINCT: without it the seed's ON CONFLICT never
                -- fires for platform-default rows. See the class docblock.
                CONSTRAINT storage_tier_policy_unique_per_scope
                    UNIQUE NULLS NOT DISTINCT (workspace_id, object_class, source_tier, target_tier)
            )
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON TABLE silver.storage_tier_policy IS
                'Tier transition rules per workspace × object class. workspace_id NULL = platform default.'
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS storage_tier_policy_workspace_active_idx
                ON silver.storage_tier_policy (workspace_id, is_active, priority)
        SQL);

        // Platform defaults (workspace_id NULL). This IS the shipped rule set —
        // the tiering agent has no built-in fallback, so an empty table means
        // nothing is ever tiered.
        DB::statement(<<<'SQL'
            INSERT INTO silver.storage_tier_policy
                (workspace_id, object_class, source_tier, target_tier, age_threshold_days, priority)
            VALUES
                (NULL, 'bronze_raw',      'hot',  'warm', 30,  100),
                (NULL, 'bronze_raw',      'warm', 'cold', 180, 110),
                (NULL, 'parser_artifact', 'hot',  'cold', 60,  120),
                (NULL, 'export_bundle',   'hot',  'warm', 14,  130),
                (NULL, 'export_bundle',   'warm', 'cold', 90,  140)
            ON CONFLICT (workspace_id, object_class, source_tier, target_tier) DO NOTHING
        SQL);

        // ── RLS ───────────────────────────────────────────────────────────
        foreach (self::MACRO_RLS_TABLES as $qualified) {
            DB::statement("ALTER TABLE {$qualified} ENABLE ROW LEVEL SECURITY");
            DB::statement("ALTER TABLE {$qualified} FORCE ROW LEVEL SECURITY");
            DB::statement("DROP POLICY IF EXISTS tenant_isolation ON {$qualified}");
            DB::statement(<<<SQL
                CREATE POLICY tenant_isolation ON {$qualified}
                    USING (
                        workspace_id IS NOT DISTINCT FROM
                            NULLIF(current_setting('app.workspace_id', true), '')::uuid
                        OR current_setting('app.workspace_id', true) IS NULL
                        OR current_setting('app.workspace_id', true) = ''
                    )
                    WITH CHECK (
                        workspace_id IS NOT DISTINCT FROM
                            NULLIF(current_setting('app.workspace_id', true), '')::uuid
                        OR current_setting('app.workspace_id', true) IS NULL
                        OR current_setting('app.workspace_id', true) = ''
                    )
                SQL);
        }

        DB::statement('ALTER TABLE '.self::TIER_POLICY_TABLE.' ENABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE '.self::TIER_POLICY_TABLE.' FORCE ROW LEVEL SECURITY');
        // Both names dropped: `tenant_isolation` is the pre-2026_08_25_200000
        // macro name, and permissive policies OR together — a leftover would
        // silently reopen the table.
        DB::statement('DROP POLICY IF EXISTS tenant_isolation ON '.self::TIER_POLICY_TABLE);
        DB::statement(
            'DROP POLICY IF EXISTS '.self::TIER_POLICY_NAME.' ON '.self::TIER_POLICY_TABLE,
        );
        // No explicit WITH CHECK: it defaults to the USING expression, which is
        // how owner sessions and migrations maintain the platform rows under
        // FORCE ROW LEVEL SECURITY. Matches 2026_08_25_200000 exactly.
        DB::statement(sprintf(
            <<<'SQL'
                CREATE POLICY %s ON %s
                    USING (
                        workspace_id IS NULL
                        OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    )
                SQL,
            self::TIER_POLICY_NAME,
            self::TIER_POLICY_TABLE,
        ));

        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app') THEN
                    GRANT USAGE ON SCHEMA silver TO georag_app;
                    GRANT SELECT, INSERT, UPDATE, DELETE
                        ON silver.store_reconciliation_findings,
                           silver.corpus_health_findings,
                           silver.storage_tier_policy
                        TO georag_app;
                END IF;
            END $$;
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach ([
            self::TIER_POLICY_TABLE,
            'silver.corpus_health_findings',
            'silver.store_reconciliation_findings',
        ] as $qualified) {
            DB::statement("DROP TABLE IF EXISTS {$qualified}");
        }
    }
};
