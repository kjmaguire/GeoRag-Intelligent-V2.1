<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Port the §4 Tool Gateway schema into the migration chain.
 *
 *   workspace.agent_risk_tiers       global tool registry + R0–R5 tier
 *   workspace.agent_permissions      per-workspace × tool allow/deny
 *   workspace.approval_requirements  per-workspace × tool required reviewer
 *   workspace.tool_invocations       audit ring of every gateway dispatch
 *
 * Declared only in `database/raw/phase0/108-section4-tool-gateway-schema.sql`,
 * which CD never runs — entries 26, 28, 30 and 33 of
 * `scripts/raw-parity-baseline.txt`.
 *
 * ## Why the registry seed is load-bearing, not decoration
 *
 * `policies.py::effective_tier` resolves a tool by joining `agent_risk_tiers`
 * and returns `None` when the row is absent, which the gateway treats as
 * "unknown tool". So on a cluster without this table, every gateway dispatch
 * fails — and with an empty table, every dispatch is rejected. The seed IS the
 * registry; it is ported verbatim rather than trimmed.
 *
 * That verbatim port includes three entries for systems since removed —
 * `query_neo4j_readonly`, `trigger_dagster_asset` and
 * `trigger_activepieces_flow`. Dropping them here would be a behaviour change,
 * not a cleanup: `impls.py::register_all_impls` still registers
 * `query_neo4j_readonly` (as a stub) and callers still reach it through
 * `invoke_tool()`, so removing its tier row turns a stub response into a hard
 * rejection. Retiring those three is a follow-up that has to change the
 * registry and the impl together.
 *
 * ## RLS
 *
 * The raw file's `toolgw_ws_isolation` policy is ported as-is (fail-open on an
 * unset GUC, matching the rest of the cluster) — none of these three tables is
 * in a verified fail-closed subset, so tightening them belongs to the tiered
 * work in `docs/architecture/fail-open-rls-posture-2026-08-21.md`.
 *
 * One deviation from the raw file, and it is required: raw writes `ENABLE ROW
 * LEVEL SECURITY` without `FORCE`. `2026_08_24_010000_force_row_level_
 * security_on_all_rls_enabled_tables` is a one-shot catalog sweep that has
 * already run, so a table created afterwards with `ENABLE` alone leaves the
 * `georag` owner bypassing its policy — and `WorkspaceRlsCoverageTest::
 * test_every_rls_enabled_table_is_forced` fails on exactly that. `FORCE` is
 * therefore added alongside every `ENABLE` below.
 *
 * `agent_risk_tiers` is intentionally global: no workspace_id, no RLS.
 *
 * Idempotent: `IF NOT EXISTS` throughout, `ON CONFLICT DO UPDATE` on the seed.
 */
return new class extends Migration
{
    private const RLS_TABLES = [
        'agent_permissions',
        'approval_requirements',
        'tool_invocations',
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS workspace.agent_risk_tiers (
                tool_name        varchar(64) PRIMARY KEY,
                risk_tier        varchar(4)  NOT NULL,
                description      text        NOT NULL,
                requires_dry_run boolean     NOT NULL DEFAULT FALSE,
                created_at       timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT agent_risk_tier_valid CHECK (
                    risk_tier IN ('R0','R1','R2','R3','R4','R5')
                )
            )
        SQL);

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS workspace.agent_permissions (
                workspace_id  uuid        NOT NULL,
                tool_name     varchar(64) NOT NULL,
                allowed       boolean     NOT NULL DEFAULT TRUE,
                override_tier varchar(4),
                notes         text,
                created_at    timestamptz NOT NULL DEFAULT now(),
                updated_at    timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (workspace_id, tool_name),
                CONSTRAINT agent_perm_override_tier_valid CHECK (
                    override_tier IS NULL OR override_tier IN ('R0','R1','R2','R3','R4','R5')
                )
            )
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_agent_perm_workspace
                ON workspace.agent_permissions (workspace_id)
        SQL);

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS workspace.approval_requirements (
                workspace_id    uuid        NOT NULL,
                tool_name       varchar(64) NOT NULL,
                required_role   varchar(40) NOT NULL DEFAULT 'qp_signoff',
                min_credentials jsonb       NOT NULL DEFAULT '{}'::jsonb,
                created_at      timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (workspace_id, tool_name)
            )
        SQL);

        // Distinct from audit.audit_ledger, which carries only R3+ actions.
        // This is the operational log for ALL gateway calls.
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS workspace.tool_invocations (
                invocation_id uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id  uuid        NOT NULL,
                actor_user_id bigint,
                actor_kind    varchar(20) NOT NULL DEFAULT 'agent',
                tool_name     varchar(64) NOT NULL,
                risk_tier     varchar(4)  NOT NULL,
                outcome       varchar(20) NOT NULL,
                block_reason  text,
                parent_run_id uuid,
                trace_id      varchar(64),
                input_hash    varchar(64),
                output_hash   varchar(64),
                duration_ms   integer,
                created_at    timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT tool_invocation_outcome_valid CHECK (
                    outcome IN ('allowed','dry_run','blocked','error')
                )
            )
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_tool_inv_workspace
                ON workspace.tool_invocations (workspace_id, created_at DESC)
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_tool_inv_tool
                ON workspace.tool_invocations (tool_name, created_at DESC)
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_tool_inv_parent
                ON workspace.tool_invocations (parent_run_id) WHERE parent_run_id IS NOT NULL
        SQL);

        // ── Registry seed — §4.2 tool list + §4.3 tiers, verbatim ─────────
        DB::statement(<<<'SQL'
            INSERT INTO workspace.agent_risk_tiers (tool_name, risk_tier, description, requires_dry_run) VALUES
                ('start_ingestion',              'R2', 'Kick off a Hatchet ingestion run for a workspace.',                FALSE),
                ('validate_schema',              'R1', 'Suggest vendor → canonical column mappings.',                      FALSE),
                ('audit_provenance',             'R0', 'Read silver.* provenance chain for a row.',                        FALSE),
                ('query_postgis_readonly',       'R0', 'Read-only PostGIS query against silver/gold/public_geo.',          FALSE),
                ('query_neo4j_readonly',         'R0', 'Read-only Cypher against the workspace graph.',                    FALSE),
                ('retrieve_qdrant',              'R0', 'Vector search against workspace + public Qdrant collections.',     FALSE),
                ('trigger_activepieces_flow',    'R3', 'Fire an external integration flow (Kestra in this build).',        TRUE),
                ('dispatch_hatchet_workflow',    'R2', 'Dispatch a registered Hatchet workflow.',                          FALSE),
                ('trigger_dagster_asset',        'R2', 'Materialise a Dagster asset on demand.',                           FALSE),
                ('generate_report',              'R2', 'Run the report builder graph for a project + template.',           FALSE),
                ('create_export',                'R4', 'Build a customer-shippable export bundle (PDF / DOCX / map pack).', TRUE),
                ('request_approval',             'R3', 'Create an approval ticket for a downstream R4/R5 action.',         FALSE),
                ('publish_arcgis',               'R4', 'Publish a layer pack to a customer ArcGIS endpoint.',              TRUE),
                ('query_public_geo',             'R0', 'Read public_geo.* layers.',                                        FALSE),
                ('create_review_item',           'R2', 'Add a row to silver.review_queue for SME triage.',                 FALSE),
                ('run_evaluation',               'R1', 'Fire the eval harness against a question_set.',                    FALSE),
                ('create_target_recommendation', 'R2', 'Insert a row into targeting.target_recommendations.',              FALSE),
                ('record_decision',              'R2', 'Insert a row into silver.decision_records.',                       FALSE),
                ('record_field_outcome',         'R2', 'Insert into targeting.target_outcomes from a field report.',       FALSE)
            ON CONFLICT (tool_name) DO UPDATE
                SET risk_tier        = EXCLUDED.risk_tier,
                    description      = EXCLUDED.description,
                    requires_dry_run = EXCLUDED.requires_dry_run
        SQL);

        // Per-workspace rows are NOT auto-populated for agent_permissions:
        // absence means ALLOW (the gateway is default-permissive). Approval
        // requirements for the two R4 tools ARE seeded eagerly so the default
        // is the safe one (QP sign-off required).
        DB::statement(<<<'SQL'
            DO $$
            DECLARE
                ws_id uuid;
            BEGIN
                FOR ws_id IN SELECT workspace_id FROM silver.workspaces LOOP
                    INSERT INTO workspace.approval_requirements
                        (workspace_id, tool_name, required_role, min_credentials)
                    VALUES
                        (ws_id, 'create_export',  'qp_signoff', '{"qp_credential_verified": true}'::jsonb),
                        (ws_id, 'publish_arcgis', 'qp_signoff', '{"qp_credential_verified": true}'::jsonb)
                    ON CONFLICT (workspace_id, tool_name) DO NOTHING;
                END LOOP;
            END $$;
        SQL);

        // ── RLS ───────────────────────────────────────────────────────────
        foreach (self::RLS_TABLES as $table) {
            $qualified = "workspace.{$table}";
            DB::statement("ALTER TABLE {$qualified} ENABLE ROW LEVEL SECURITY");
            DB::statement("ALTER TABLE {$qualified} FORCE ROW LEVEL SECURITY");
            DB::statement("DROP POLICY IF EXISTS toolgw_ws_isolation ON {$qualified}");
            DB::statement(<<<SQL
                CREATE POLICY toolgw_ws_isolation ON {$qualified}
                    USING (
                        workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                        OR NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                    )
                    WITH CHECK (
                        workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    )
                SQL);
        }

        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app') THEN
                    GRANT USAGE ON SCHEMA workspace TO georag_app;
                    GRANT SELECT ON workspace.agent_risk_tiers TO georag_app;
                    GRANT SELECT, INSERT, UPDATE, DELETE
                        ON workspace.agent_permissions,
                           workspace.approval_requirements,
                           workspace.tool_invocations
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
            'workspace.tool_invocations',
            'workspace.approval_requirements',
            'workspace.agent_permissions',
            'workspace.agent_risk_tiers',
        ] as $qualified) {
            DB::statement("DROP TABLE IF EXISTS {$qualified}");
        }
    }
};
