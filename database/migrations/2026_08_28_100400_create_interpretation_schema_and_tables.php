<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Port the §19.3 Interpretation Workspace schema into the migration chain.
 *
 *   interpretation.interpretation_notes          per-project notes, optional point anchor
 *   interpretation.interpretation_section_lines  drawn cross-section traces (LineString)
 *   interpretation.interpretation_target_zones   drawn target polygons, with an accept workflow
 *   interpretation.interpretation_comments       threaded comments on any of the above
 *
 * Declared only in `database/raw/phase0/107-section19-3-interpretation-schema.sql`,
 * which CD never runs — four consecutive entries in
 * `scripts/raw-parity-baseline.txt`. The SCHEMA is raw-only too, so this
 * migration creates it rather than assuming it.
 *
 * ## Why this one is not latent
 *
 * Unlike most of the raw-only set, `src/fastapi/app/routers/interpretation.py`
 * is fully implemented and mounted — `app.include_router(interpretation_router
 * .router)` in main.py. Every one of its endpoints issues SQL against these
 * four tables, so on Azure each is a `42P01 undefined_table` today, not a
 * dormant capability. The router's INSERT column lists and RETURNING clauses
 * were diffed against this DDL before porting: `note_id`, `anchor_geom`,
 * `tags`, `section_id`, `azimuth_deg`, `zone_id`, `accepted*`,
 * `parent_comment_id`, `target_table`/`target_id` all match.
 *
 * (The feature still has no frontend. That is a separate gap — this migration
 * makes the API surface work, it does not make the feature reachable from the
 * product.)
 *
 * ## PostGIS
 *
 * All three geometry columns are SRID 4326 and the router writes them through
 * `ST_SetSRID(ST_GeomFromGeoJSON(...), 4326)`, so the typed columns are load-
 * bearing rather than decorative — a `geometry` column without the type/SRID
 * modifier would silently accept a mis-projected write. GiST indexes are
 * created alongside, matching the raw file.
 *
 * ## RLS
 *
 * `interp_ws_isolation` is ported in the raw file's shape: fail-open on an
 * unset `app.workspace_id`, with a strict `WITH CHECK` so a write always names
 * its workspace. None of these tables is in a verified fail-closed subset, so
 * tightening them belongs with the tiered work in
 * `docs/architecture/fail-open-rls-posture-2026-08-21.md`.
 *
 * One deviation, and it is required: the raw file writes `ENABLE ROW LEVEL
 * SECURITY` without `FORCE`. The catalog sweep in
 * `2026_08_24_010000_force_row_level_security_on_all_rls_enabled_tables` is
 * one-shot and already ran, so a table created afterwards with `ENABLE` alone
 * leaves the `georag` owner bypassing its policy — which
 * `WorkspaceRlsCoverageTest::test_every_rls_enabled_table_is_forced` asserts
 * against. `FORCE` is added alongside every `ENABLE`.
 *
 * `2026_05_25_185013_normalize_layered_workspace_isolation_policies_phase2`
 * lists these four tables, but it is catalog-driven — it rewrites policies
 * carrying three or more `NULLIF` clauses. The single policy created here has
 * two, so that migration would have been a no-op for these tables and needs no
 * reconciliation.
 *
 * ## One index the raw file does not have
 *
 * `interpretation_comments` is the only one of the four with no index covering
 * `workspace_id` — the raw file indexes `(target_table, target_id)` and
 * `(parent_comment_id)` and stops there, while its three siblings all get
 * `(workspace_id, project_id)`. That is a gap rather than a decision: every
 * read of the table is filtered by `workspace_id` through
 * `interp_ws_isolation`, so without an index each policy evaluation is a
 * sequential scan of the whole comment table.
 * `idx_interp_comments_workspace` is added here in the siblings' shape.
 *
 * (Correction to an earlier draft of this docblock: the §11.5 index gate in
 * `routers/audit_findings.py` would NOT have flagged it. That check scans
 * `silver`/`gold`/`audit`/`ops`/`workflow`/`targeting` only — `interpretation`
 * is not among its `_TENANT_SCHEMAS`, so the table is outside its reach
 * entirely. The index is justified by the sequential scan alone, which is
 * reason enough; the gate was not going to catch it.)
 *
 * Idempotent: `CREATE SCHEMA/TABLE/INDEX IF NOT EXISTS`, `DROP POLICY IF
 * EXISTS` before each `CREATE POLICY`.
 *
 * ## Split ownership: the one case where this migration stands down
 *
 * `interpretation` is one of only three schemas that `database/raw/phase0/`
 * creates for itself (with `backups` and `gold`), so it is the one case where
 * an operator who has run `php artisan db:apply-raw` already has the schema
 * AND all four tables — owned by whatever role ran that, which is not
 * necessarily the role that runs migrations. The `Migrations under production
 * privileges` CI job reproduces exactly this: `bootstrap` applies
 * `database/raw/phase0/*.sql`, then `georag_ci` — deliberately not a member of
 * `bootstrap` and deliberately not a superuser — runs `php artisan migrate`.
 * Against that cluster `CREATE TABLE IF NOT EXISTS` does not quietly skip; it
 * fails with `permission denied for schema interpretation`, because Postgres
 * resolves the creation namespace (and checks CREATE on it) before it ever
 * reaches the IF NOT EXISTS test.
 *
 * Azure is not that cluster — the schema does not exist there at all, so the
 * normal path creates everything under the migration role and every ALTER
 * below is an ALTER of a table this role owns. But a compose or on-prem box
 * where raw was applied by hand IS that cluster, and there is genuinely
 * nothing this migration can do there: `CREATE INDEX`, `ALTER TABLE ... FORCE
 * ROW LEVEL SECURITY` and `CREATE POLICY` all require ownership, not a grant.
 * So it stands down with a `RAISE WARNING` naming the remedy, in the same
 * shape as the sweep in `2026_08_24_010000`, which skips foreign-owned tables
 * with `pg_has_role(current_user, c.relowner, 'USAGE')` and warns. Standing
 * down loses only the reconciliations — the tables themselves are already
 * there, in the raw file's shape.
 *
 * Partial ownership is NOT tolerated: some tables present and foreign-owned
 * while others are missing means the cluster is in a state no supported path
 * produces, and the migration throws rather than leaving half a schema behind.
 */
return new class extends Migration
{
    private const TABLES = [
        'interpretation_notes',
        'interpretation_section_lines',
        'interpretation_target_zones',
        'interpretation_comments',
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('CREATE SCHEMA IF NOT EXISTS interpretation');

        if ($this->standDownForForeignOwnership()) {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS interpretation.interpretation_notes (
                note_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id   uuid NOT NULL,
                project_id     uuid,
                author_user_id bigint NOT NULL,
                title          varchar(200),
                body_md        text NOT NULL,
                anchor_geom    geometry(Point, 4326),
                tags           text[] NOT NULL DEFAULT ARRAY[]::text[],
                created_at     timestamptz NOT NULL DEFAULT now(),
                updated_at     timestamptz NOT NULL DEFAULT now()
            )
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_interp_notes_workspace
                ON interpretation.interpretation_notes (workspace_id, project_id)
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_interp_notes_anchor
                ON interpretation.interpretation_notes USING gist (anchor_geom)
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_interp_notes_tags
                ON interpretation.interpretation_notes USING gin (tags)
        SQL);

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS interpretation.interpretation_section_lines (
                section_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id   uuid NOT NULL,
                project_id     uuid,
                author_user_id bigint NOT NULL,
                name           varchar(200),
                azimuth_deg    numeric(6,2),
                geom           geometry(LineString, 4326) NOT NULL,
                notes          text,
                created_at     timestamptz NOT NULL DEFAULT now(),
                updated_at     timestamptz NOT NULL DEFAULT now()
            )
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_interp_section_workspace
                ON interpretation.interpretation_section_lines (workspace_id, project_id)
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_interp_section_geom
                ON interpretation.interpretation_section_lines USING gist (geom)
        SQL);

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS interpretation.interpretation_target_zones (
                zone_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id   uuid NOT NULL,
                project_id     uuid,
                author_user_id bigint NOT NULL,
                name           varchar(200) NOT NULL,
                rationale      text,
                commodity      varchar(64),
                confidence     varchar(16) NOT NULL DEFAULT 'medium',
                geom           geometry(Polygon, 4326) NOT NULL,
                accepted       boolean NOT NULL DEFAULT FALSE,
                accepted_by    bigint,
                accepted_at    timestamptz,
                created_at     timestamptz NOT NULL DEFAULT now(),
                updated_at     timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT interp_zone_confidence_chk CHECK (confidence IN ('low','medium','high'))
            )
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_interp_zone_workspace
                ON interpretation.interpretation_target_zones (workspace_id, project_id)
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_interp_zone_geom
                ON interpretation.interpretation_target_zones USING gist (geom)
        SQL);

        // Threaded: parent_comment_id self-references, cascading so deleting a
        // root comment removes its replies.
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS interpretation.interpretation_comments (
                comment_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id      uuid NOT NULL,
                project_id        uuid,
                author_user_id    bigint NOT NULL,
                parent_comment_id uuid
                    REFERENCES interpretation.interpretation_comments(comment_id) ON DELETE CASCADE,
                target_table      varchar(64) NOT NULL,
                target_id         uuid NOT NULL,
                body_md           text NOT NULL,
                created_at        timestamptz NOT NULL DEFAULT now(),
                updated_at        timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT interp_comment_target_chk CHECK (
                    target_table IN (
                        'interpretation_notes',
                        'interpretation_section_lines',
                        'interpretation_target_zones'
                    )
                )
            )
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_interp_comments_target
                ON interpretation.interpretation_comments (target_table, target_id)
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_interp_comments_thread
                ON interpretation.interpretation_comments (parent_comment_id)
        SQL);
        // Not in the raw file — see the class docblock. The other three
        // tables have a (workspace_id, project_id) index; this one was
        // missed, and every read of it is filtered by workspace_id through
        // interp_ws_isolation.
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_interp_comments_workspace
                ON interpretation.interpretation_comments (workspace_id, project_id)
        SQL);

        foreach (self::TABLES as $table) {
            $qualified = "interpretation.{$table}";

            DB::statement("ALTER TABLE {$qualified} ENABLE ROW LEVEL SECURITY");
            DB::statement("ALTER TABLE {$qualified} FORCE ROW LEVEL SECURITY");
            DB::statement("DROP POLICY IF EXISTS interp_ws_isolation ON {$qualified}");
            DB::statement(<<<SQL
                CREATE POLICY interp_ws_isolation ON {$qualified}
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
                    GRANT USAGE ON SCHEMA interpretation TO georag_app;
                    GRANT SELECT, INSERT, UPDATE, DELETE
                        ON interpretation.interpretation_notes,
                           interpretation.interpretation_section_lines,
                           interpretation.interpretation_target_zones,
                           interpretation.interpretation_comments
                        TO georag_app;
                END IF;
            END $$;
        SQL);
    }

    /**
     * True when every table already exists under an owner this role is not a
     * member of — the `db:apply-raw`-then-migrate cluster described in the
     * class docblock. Emits a WARNING naming the remedy and lets up() return.
     *
     * Throws instead when ownership is split part-way, which no supported
     * path produces and which would leave the schema half-built.
     */
    private function standDownForForeignOwnership(): bool
    {
        $missing = [];
        $foreign = [];

        foreach (self::TABLES as $table) {
            $qualified = "interpretation.{$table}";

            // Looked up through pg_class/pg_namespace by NAME rather than
            // to_regclass(): resolving a qualified name needs USAGE on the
            // schema, which is precisely what this role does not have on a
            // bootstrap-owned `interpretation`. Reading the catalogs does not.
            $row = DB::selectOne(
                <<<'SQL'
                SELECT pg_has_role(current_user, c.relowner, 'USAGE') AS mine,
                       pg_get_userbyid(c.relowner)                    AS owner
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'interpretation' AND c.relname = ?
                SQL,
                [$table],
            );

            if ($row === null) {
                $missing[] = $qualified;
            } elseif (! $row->mine) {
                $foreign[] = "{$qualified} (owned by {$row->owner})";
            }
        }

        if ($foreign === []) {
            return false;
        }

        if ($missing !== []) {
            throw new \RuntimeException(
                "interpretation.* is split across owners: ".implode(', ', $foreign)
                ." already exist under another role while ".implode(', ', $missing)
                ." are missing. No supported path produces this. Reassign ownership of the
"
                ."existing tables to the migration role, or drop them and re-run.",
            );
        }

        // All four present, none of them ours. Nothing here is grantable —
        // CREATE INDEX, ALTER TABLE and CREATE POLICY all require ownership.
        DB::statement(
            <<<'SQL'
            DO $$
            DECLARE
                owner_name text;
            BEGIN
                SELECT pg_get_userbyid(c.relowner) INTO owner_name
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'interpretation'
                   AND c.relname = 'interpretation_notes';

                RAISE WARNING 'interpretation.* already exists under another role (%) — '
                    'skipping. This cluster applied database/raw/phase0/107 before '
                    'migrating, so the four tables are present in the raw file''s shape '
                    'but WITHOUT idx_interp_comments_workspace and WITHOUT FORCE ROW LEVEL '
                    'SECURITY. Re-run this migration as the owning role, or reassign '
                    'ownership to the migration role, to pick those up.', owner_name;
            END $$;
            SQL
        );

        return true;
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // Only drop what this role owns. On the split-ownership cluster the
        // class docblock describes, up() stood down without creating anything,
        // so there is nothing here to reverse — and DROP TABLE would fail with
        // `must be owner` rather than no-op.
        foreach ([
            'interpretation.interpretation_comments',
            'interpretation.interpretation_target_zones',
            'interpretation.interpretation_section_lines',
            'interpretation.interpretation_notes',
        ] as $qualified) {
            $mine = DB::selectOne(
                "SELECT pg_has_role(current_user, c.relowner, 'USAGE') AS mine
                   FROM pg_class c
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                  WHERE n.nspname = 'interpretation' AND c.relname = ?",
                [substr($qualified, strlen('interpretation.'))],
            );

            if ($mine !== null && ! $mine->mine) {
                continue;
            }

            DB::statement("DROP TABLE IF EXISTS {$qualified}");
        }

        // The schema is created by this migration, so it is dropped here —
        // but only if empty, in case a later migration added a table to it,
        // and only if this role owns it. On the split-ownership cluster the
        // class docblock describes, `bootstrap` owns the schema and DROP
        // SCHEMA fails with `must be owner` rather than no-op.
        $ownsSchema = DB::selectOne(
            "SELECT pg_has_role(current_user, n.nspowner, 'USAGE') AS mine
               FROM pg_namespace n WHERE n.nspname = 'interpretation'",
        );

        if ($ownsSchema === null || $ownsSchema->mine) {
            DB::statement('DROP SCHEMA IF EXISTS interpretation RESTRICT');
        }
    }
};
