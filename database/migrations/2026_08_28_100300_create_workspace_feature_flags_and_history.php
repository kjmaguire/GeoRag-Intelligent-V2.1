<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Port `workspace.feature_flags`, `workspace.feature_flag_history` and the
 * `workspace.feature_flags_audit()` trigger function into the migration chain.
 *
 * Declared only in `database/raw/phase1/20-shadow-runs-and-feature-flags.sql`
 * and `phase1/30-feature-flag-history.sql`, neither of which CD runs — entries
 * 29, 31 and 32 of `scripts/raw-parity-baseline.txt` (the third being the
 * function).
 *
 * ## What this is
 *
 * A second, database-backed feature-flag layer, entirely separate from the
 * ~125 environment-variable settings in `src/fastapi/app/config.py`. Flags
 * resolve workspace-first with a `workspace_id IS NULL` row as the platform
 * default. `public_geoscience_pull` and `external_notification` read it on
 * every run, and `workflow.flow_registry.flag_name` carries a CHECK constraint
 * pinning names to `flows.<name>.enabled` — a foreign concept with nothing to
 * point at until this table exists.
 *
 * ## The seed is deliberately NOT ported
 *
 * `phase1/20` seeds two rows — `ingest_pdf_hatchet_traffic_pct` and
 * `ingest_pdf_shadow_enabled` — for the Phase 1 shadow ramp. A later raw file,
 * `phase4/90-drop-shadow-runs.sql`, DELETEs both because that ramp closed.
 * Porting the seed would resurrect two dead flags whose only reader
 * (`ShadowRouter`) is itself no longer called. The table is created empty.
 *
 * ## RLS — the raw shape, which is NULL-visible by design
 *
 * Both tables carry `tenant_isolation` exactly as the raw files write it. It
 * is fail-open on an unset GUC, and additionally admits `workspace_id IS NULL`
 * rows unconditionally — that second clause is not an oversight, it is what
 * makes the platform-default flag readable from a workspace-bound session.
 * Neither table is in a verified fail-closed subset, and closing them without
 * preserving platform-default visibility would break flag resolution for every
 * tenant, so any tightening belongs with the tiered work in
 * `docs/architecture/fail-open-rls-posture-2026-08-21.md`.
 *
 * `feature_flag_history` keeps `WITH CHECK (true)`: only the trigger writes it,
 * and the trigger runs SECURITY DEFINER after the row it describes has already
 * passed the flag table's own check.
 *
 * `FORCE` is set alongside `ENABLE` on both — the catalog sweep in
 * `2026_08_24_010000` has already run and will not revisit new tables.
 *
 * Idempotent: `IF NOT EXISTS`, `CREATE OR REPLACE FUNCTION`, and
 * `DROP TRIGGER IF EXISTS` before `CREATE TRIGGER`.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // NULLS NOT DISTINCT (PG15+) so the platform-default row
        // (workspace_id IS NULL) is a single key under ON CONFLICT. Without
        // it the UPSERT path silently inserts duplicates, because NULL <> NULL
        // in standard SQL.
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS workspace.feature_flags (
                id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id uuid        NULL
                    REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                flag_name    text        NOT NULL,
                bool_value   boolean     NULL,
                int_value    integer     NULL,
                string_value text        NULL,
                json_value   jsonb       NULL,
                description  text        NULL,
                created_at   timestamptz NOT NULL DEFAULT now(),
                updated_at   timestamptz NOT NULL DEFAULT now(),
                updated_by   bigint      NULL,
                CONSTRAINT feature_flags_unique UNIQUE NULLS NOT DISTINCT (workspace_id, flag_name),
                CONSTRAINT feature_flags_one_value CHECK (
                    (CASE WHEN bool_value   IS NOT NULL THEN 1 ELSE 0 END +
                     CASE WHEN int_value    IS NOT NULL THEN 1 ELSE 0 END +
                     CASE WHEN string_value IS NOT NULL THEN 1 ELSE 0 END +
                     CASE WHEN json_value   IS NOT NULL THEN 1 ELSE 0 END) >= 1
                )
            )
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON TABLE workspace.feature_flags IS
                'Per-workspace feature toggles. workspace_id NULL = platform-wide default.'
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS feature_flags_workspace_idx
                ON workspace.feature_flags (workspace_id, flag_name)
        SQL);

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS workspace.feature_flag_history (
                id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
                flag_id          uuid        NOT NULL,
                workspace_id     uuid        NULL,
                flag_name        text        NOT NULL,
                op               text        NOT NULL CHECK (op IN ('INSERT','UPDATE','DELETE')),
                old_bool_value   boolean     NULL,
                old_int_value    integer     NULL,
                old_string_value text        NULL,
                old_json_value   jsonb       NULL,
                new_bool_value   boolean     NULL,
                new_int_value    integer     NULL,
                new_string_value text        NULL,
                new_json_value   jsonb       NULL,
                actor_id         bigint      NULL,
                changed_at       timestamptz NOT NULL DEFAULT clock_timestamp()
            )
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON TABLE workspace.feature_flag_history IS
                'Append-only audit trail for workspace.feature_flags. One row per flag mutation.'
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN workspace.feature_flag_history.actor_id IS
                'Read from the app.actor_id GUC at trigger fire time. NULL when the caller did not stamp it.'
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS feature_flag_history_flag_idx
                ON workspace.feature_flag_history (flag_name, changed_at DESC)
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS feature_flag_history_workspace_idx
                ON workspace.feature_flag_history (workspace_id, changed_at DESC)
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS feature_flag_history_changed_at_idx
                ON workspace.feature_flag_history (changed_at DESC)
        SQL);

        // ── RLS ───────────────────────────────────────────────────────────
        DB::statement('ALTER TABLE workspace.feature_flags ENABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE workspace.feature_flags FORCE ROW LEVEL SECURITY');
        DB::statement('DROP POLICY IF EXISTS tenant_isolation ON workspace.feature_flags');
        DB::statement(<<<'SQL'
            CREATE POLICY tenant_isolation ON workspace.feature_flags
                USING (
                    workspace_id IS NULL
                    OR workspace_id IS NOT DISTINCT FROM
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

        DB::statement('ALTER TABLE workspace.feature_flag_history ENABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE workspace.feature_flag_history FORCE ROW LEVEL SECURITY');
        DB::statement('DROP POLICY IF EXISTS tenant_isolation ON workspace.feature_flag_history');
        DB::statement(<<<'SQL'
            CREATE POLICY tenant_isolation ON workspace.feature_flag_history
                USING (
                    workspace_id IS NULL
                    OR workspace_id IS NOT DISTINCT FROM
                       NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    OR current_setting('app.workspace_id', true) IS NULL
                    OR current_setting('app.workspace_id', true) = ''
                )
                WITH CHECK (true)
        SQL);

        // ── Audit trigger ─────────────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION workspace.feature_flags_audit() RETURNS trigger
                LANGUAGE plpgsql
                SECURITY DEFINER
                SET search_path = workspace, pg_catalog
            AS $fn$
            DECLARE
                actor_setting text := current_setting('app.actor_id', true);
                actor_bigint  bigint := NULL;
            BEGIN
                -- app.actor_id may be unset (empty string) or a numeric string.
                -- Anything non-numeric maps to NULL rather than raising.
                IF actor_setting IS NOT NULL AND actor_setting <> '' THEN
                    BEGIN
                        actor_bigint := actor_setting::bigint;
                    EXCEPTION WHEN OTHERS THEN
                        actor_bigint := NULL;
                    END;
                END IF;

                IF TG_OP = 'INSERT' THEN
                    INSERT INTO workspace.feature_flag_history (
                        flag_id, workspace_id, flag_name, op,
                        new_bool_value, new_int_value, new_string_value, new_json_value,
                        actor_id
                    ) VALUES (
                        NEW.id, NEW.workspace_id, NEW.flag_name, 'INSERT',
                        NEW.bool_value, NEW.int_value, NEW.string_value, NEW.json_value,
                        COALESCE(actor_bigint, NEW.updated_by)
                    );
                    RETURN NEW;

                ELSIF TG_OP = 'UPDATE' THEN
                    -- Skip no-op UPDATEs (timestamp-only churn).
                    IF NEW.bool_value       IS NOT DISTINCT FROM OLD.bool_value
                       AND NEW.int_value    IS NOT DISTINCT FROM OLD.int_value
                       AND NEW.string_value IS NOT DISTINCT FROM OLD.string_value
                       AND NEW.json_value   IS NOT DISTINCT FROM OLD.json_value
                    THEN
                        RETURN NEW;
                    END IF;

                    INSERT INTO workspace.feature_flag_history (
                        flag_id, workspace_id, flag_name, op,
                        old_bool_value, old_int_value, old_string_value, old_json_value,
                        new_bool_value, new_int_value, new_string_value, new_json_value,
                        actor_id
                    ) VALUES (
                        NEW.id, NEW.workspace_id, NEW.flag_name, 'UPDATE',
                        OLD.bool_value, OLD.int_value, OLD.string_value, OLD.json_value,
                        NEW.bool_value, NEW.int_value, NEW.string_value, NEW.json_value,
                        COALESCE(actor_bigint, NEW.updated_by)
                    );
                    RETURN NEW;

                ELSIF TG_OP = 'DELETE' THEN
                    INSERT INTO workspace.feature_flag_history (
                        flag_id, workspace_id, flag_name, op,
                        old_bool_value, old_int_value, old_string_value, old_json_value,
                        actor_id
                    ) VALUES (
                        OLD.id, OLD.workspace_id, OLD.flag_name, 'DELETE',
                        OLD.bool_value, OLD.int_value, OLD.string_value, OLD.json_value,
                        COALESCE(actor_bigint, OLD.updated_by)
                    );
                    RETURN OLD;
                END IF;

                RETURN NULL;
            END;
            $fn$
        SQL);

        DB::statement('DROP TRIGGER IF EXISTS feature_flags_audit_trg ON workspace.feature_flags');
        DB::statement(<<<'SQL'
            CREATE TRIGGER feature_flags_audit_trg
                AFTER INSERT OR UPDATE OR DELETE ON workspace.feature_flags
                FOR EACH ROW EXECUTE FUNCTION workspace.feature_flags_audit()
        SQL);

        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app') THEN
                    GRANT USAGE ON SCHEMA workspace TO georag_app;
                    GRANT SELECT, INSERT, UPDATE, DELETE ON workspace.feature_flags TO georag_app;
                    GRANT SELECT, INSERT ON workspace.feature_flag_history TO georag_app;
                END IF;
            END $$;
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP TRIGGER IF EXISTS feature_flags_audit_trg ON workspace.feature_flags');
        DB::statement('DROP FUNCTION IF EXISTS workspace.feature_flags_audit()');
        DB::statement('DROP TABLE IF EXISTS workspace.feature_flag_history');
        DB::statement('DROP TABLE IF EXISTS workspace.feature_flags');
    }
};
