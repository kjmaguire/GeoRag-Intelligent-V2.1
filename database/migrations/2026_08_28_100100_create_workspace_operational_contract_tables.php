<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Port the six Layer-E operational-contract tables into the migration chain.
 *
 *   workspace.agent_timeouts          per-agent timeout + circuit-breaker policy
 *   workspace.prompt_versions         every prompt version + promotion state
 *   workspace.agent_prompt_pins       per-agent pinned prompt version
 *   workspace.workspace_agent_config  per-workspace agent parameter overrides
 *   workspace.idempotency_keys        R2+ invocation dedupe
 *   workspace.dry_run_outputs         R3+ dry-run side-effect capture
 *
 * All six are declared only in
 * `database/raw/phase0/50-layer-e-operational-contract.sql`, which CD never
 * runs, so none has ever existed on Azure — entries 24–30 of
 * `scripts/raw-parity-baseline.txt`.
 *
 * ## What this unblocks
 *
 * `app/agents/wrapper.py` reads `agent_timeouts` on every agent invocation and
 * writes `idempotency_keys` and `dry_run_outputs`; `idempotency_keys_cleanup`
 * is a nightly Hatchet cron (04:15 UTC) whose whole body is a DELETE against a
 * table that is not there; `llm_incident_diagnosis` and `support_packet` read
 * `prompt_versions` and `agent_prompt_pins`. `database/seeders/
 * Phase0AgentTimeoutsSeeder.php` has had nothing to seed into.
 *
 * ## RLS — three different answers, all deliberate
 *
 * - `workspace_agent_config` and `dry_run_outputs` are in the verified
 *   fail-closed subset of `2026_08_14_030000_close_rls_admin_escape_hatch_
 *   verified_subset` (and of `WorkspaceRlsCoverageTest::
 *   test_verified_subset_has_no_fail_open_escape_hatch`). That migration skips
 *   them because they do not exist, so they are created already closed here,
 *   in the shape it writes.
 * - `idempotency_keys` gets RLS from `phase0/95-rls-policies.sql` but is in
 *   NO fail-closed subset, so it keeps the fail-open shape that file installs.
 *   Flipping it belongs to the tiered work in
 *   `docs/architecture/fail-open-rls-posture-2026-08-21.md`, not here.
 * - `agent_timeouts`, `prompt_versions` and `agent_prompt_pins` get NO RLS.
 *   That is not an omission: 95-rls-policies.sql names all three under
 *   "Tables that DO NOT get RLS ... (global config)", and none of them has a
 *   `workspace_id` column to scope on.
 *
 * Every RLS-enabled table gets `FORCE` alongside `ENABLE` — the catalog sweep
 * in `2026_08_24_010000` already ran, so a table created afterwards with only
 * `ENABLE` leaves the owner role bypassing its policy.
 *
 * Idempotent throughout.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // ── Global config: no workspace_id, no RLS ────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS workspace.agent_timeouts (
                agent_name            text        PRIMARY KEY,
                risk_tier             text        NOT NULL DEFAULT 'R0'
                    CHECK (risk_tier IN ('R0','R1','R2','R3','R4','R5')),
                soft_timeout_ms       integer     NOT NULL DEFAULT 30000,
                hard_timeout_ms       integer     NOT NULL DEFAULT 120000,
                retry_count           smallint    NOT NULL DEFAULT 1,
                circuit_breaker_scope text        NOT NULL DEFAULT 'workspace'
                    CHECK (circuit_breaker_scope IN ('none','workspace','global')),
                failure_threshold     smallint    NOT NULL DEFAULT 5,
                cool_down_seconds     integer     NOT NULL DEFAULT 300,
                updated_at            timestamptz NOT NULL DEFAULT now(),
                updated_by            bigint      NULL,
                CONSTRAINT agent_timeouts_soft_lt_hard CHECK (soft_timeout_ms <= hard_timeout_ms)
            )
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON TABLE workspace.agent_timeouts IS
                'Per-agent timeout + retry + circuit-breaker policy, read by the wrapper on every invocation.'
        SQL);

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS workspace.prompt_versions (
                id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
                prompt_id       text        NOT NULL,
                version         text        NOT NULL,
                text            text        NOT NULL,
                parameters      jsonb       NOT NULL DEFAULT '{}'::jsonb,
                promotion_state text        NOT NULL DEFAULT 'draft'
                    CHECK (promotion_state IN ('draft','staging','production','deprecated')),
                promoted_at     timestamptz NULL,
                deprecated_at   timestamptz NULL,
                created_at      timestamptz NOT NULL DEFAULT now(),
                created_by      bigint      NULL,
                notes           text        NULL,
                CONSTRAINT prompt_versions_prompt_id_version UNIQUE (prompt_id, version)
            )
        SQL);
        // At most one production version per prompt_id at a time.
        DB::statement(<<<'SQL'
            CREATE UNIQUE INDEX IF NOT EXISTS prompt_versions_one_production_per_prompt
                ON workspace.prompt_versions (prompt_id) WHERE promotion_state = 'production'
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS prompt_versions_prompt_id_idx
                ON workspace.prompt_versions (prompt_id, created_at DESC)
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON TABLE workspace.prompt_versions IS
                'Every prompt version authored, with promotion lifecycle. Resolved via agent_prompt_pins or the production-promoted row.'
        SQL);

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS workspace.agent_prompt_pins (
                agent_name        text        PRIMARY KEY,
                prompt_id         text        NOT NULL,
                prompt_version_id uuid        NULL
                    REFERENCES workspace.prompt_versions(id) ON DELETE SET NULL,
                pinned_at         timestamptz NULL,
                pinned_by         bigint      NULL,
                updated_at        timestamptz NOT NULL DEFAULT now()
            )
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON TABLE workspace.agent_prompt_pins IS
                'Per-agent prompt-version pin. NULL pin resolves to the production-promoted version.'
        SQL);

        // ── Workspace-scoped: RLS below ───────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS workspace.workspace_agent_config (
                id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id uuid        NOT NULL
                    REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                agent_name   text        NOT NULL,
                config       jsonb       NOT NULL DEFAULT '{}'::jsonb,
                enabled      boolean     NOT NULL DEFAULT true,
                updated_at   timestamptz NOT NULL DEFAULT now(),
                updated_by   bigint      NULL,
                CONSTRAINT workspace_agent_config_workspace_agent UNIQUE (workspace_id, agent_name)
            )
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS workspace_agent_config_workspace_idx
                ON workspace.workspace_agent_config (workspace_id)
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON TABLE workspace.workspace_agent_config IS
                'Per-workspace overrides of agent parameters and enable/disable toggles.'
        SQL);

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS workspace.idempotency_keys (
                id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
                key_hash       bytea       NOT NULL UNIQUE,
                key_components jsonb       NOT NULL,
                risk_tier      text        NOT NULL CHECK (risk_tier IN ('R2','R3','R4','R5')),
                workspace_id   uuid        NULL,
                agent_name     text        NOT NULL,
                agent_version  text        NULL,
                invocation_id  uuid        NULL,
                result_summary jsonb       NULL,
                outcome        text        NULL
                    CHECK (outcome IN ('success','refusal','failure','timeout','circuit_open')),
                created_at     timestamptz NOT NULL DEFAULT now(),
                expires_at     timestamptz NULL
            )
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idempotency_keys_workspace_agent_idx
                ON workspace.idempotency_keys (workspace_id, agent_name, created_at DESC)
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idempotency_keys_expires_idx
                ON workspace.idempotency_keys (expires_at) WHERE expires_at IS NOT NULL
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON TABLE workspace.idempotency_keys IS
                'Idempotency dedupe for R2+ agent invocations. Lookup by key_hash; key_components for auditability.'
        SQL);

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS workspace.dry_run_outputs (
                id                     uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
                invocation_id          uuid        NOT NULL,
                workspace_id           uuid        NOT NULL
                    REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                agent_name             text        NOT NULL,
                target                 text        NOT NULL,
                payload                jsonb       NOT NULL,
                would_have_executed_at timestamptz NOT NULL DEFAULT now(),
                created_at             timestamptz NOT NULL DEFAULT now()
            )
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS dry_run_outputs_invocation_idx
                ON workspace.dry_run_outputs (invocation_id)
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS dry_run_outputs_workspace_idx
                ON workspace.dry_run_outputs (workspace_id, created_at DESC)
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON TABLE workspace.dry_run_outputs IS
                'Captured side-effect calls from dry-run agent invocations. None of these are executed.'
        SQL);

        // ── RLS ───────────────────────────────────────────────────────────
        // Fail-closed: the shape 2026_08_14_030000 writes for this subset.
        foreach (['workspace_agent_config', 'dry_run_outputs'] as $table) {
            $qualified = "workspace.{$table}";
            DB::statement("ALTER TABLE {$qualified} ENABLE ROW LEVEL SECURITY");
            DB::statement("ALTER TABLE {$qualified} FORCE ROW LEVEL SECURITY");
            DB::statement("DROP POLICY IF EXISTS tenant_isolation ON {$qualified}");
            DB::statement(<<<SQL
                CREATE POLICY tenant_isolation ON {$qualified}
                    USING (
                        workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    )
                    WITH CHECK (
                        workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    )
                SQL);
        }

        // Fail-open: the canonical phase0/95 shape. idempotency_keys is not in
        // any verified fail-closed subset, and its workspace_id is nullable —
        // the wrapper writes rows for platform-level invocations too.
        DB::statement('ALTER TABLE workspace.idempotency_keys ENABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE workspace.idempotency_keys FORCE ROW LEVEL SECURITY');
        DB::statement('DROP POLICY IF EXISTS tenant_isolation ON workspace.idempotency_keys');
        DB::statement(<<<'SQL'
            CREATE POLICY tenant_isolation ON workspace.idempotency_keys
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

        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app') THEN
                    -- USAGE is granted here rather than assumed: the only place
                    -- that grants it today is database/raw/phase1/10-georag-app-role.sql,
                    -- which is in neither the migration chain nor raw/manifest.json,
                    -- so no automated path applies it. Idempotent and harmless where
                    -- it is already held.
                    GRANT USAGE ON SCHEMA workspace TO georag_app;
                    GRANT SELECT ON workspace.agent_timeouts, workspace.prompt_versions,
                                    workspace.agent_prompt_pins TO georag_app;
                    GRANT SELECT, INSERT, UPDATE, DELETE
                        ON workspace.workspace_agent_config,
                           workspace.idempotency_keys,
                           workspace.dry_run_outputs
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

        // agent_prompt_pins FKs prompt_versions; drop it first.
        foreach ([
            'workspace.dry_run_outputs',
            'workspace.idempotency_keys',
            'workspace.workspace_agent_config',
            'workspace.agent_prompt_pins',
            'workspace.prompt_versions',
            'workspace.agent_timeouts',
        ] as $qualified) {
            DB::statement("DROP TABLE IF EXISTS {$qualified}");
        }
    }
};
