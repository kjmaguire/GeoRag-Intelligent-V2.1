<?php

namespace Tests;

use Illuminate\Foundation\Testing\TestCase as BaseTestCase;

abstract class TestCase extends BaseTestCase
{
    /**
     * Skip the current test when the default DB connection is sqlite — use in
     * tests that exercise PostGIS (ST_*, geometry columns) or query PG-only
     * catalogs (information_schema.*). These require the Postgres test
     * connection to be configured.
     */
    protected function skipIfSqlite(string $reason = 'Requires PostGIS / PostgreSQL features not available on SQLite.'): void
    {
        if (config('database.default') === 'sqlite') {
            $this->markTestSkipped($reason);
        }
    }

    /**
     * Override refreshApplication() to install the SQLite compatibility hook
     * on the database connection immediately after the application is created.
     *
     * This approach is required because PHP's trait method resolution gives
     * trait methods (RefreshDatabase::beforeRefreshingDatabase) higher
     * precedence than inherited class methods, making it impossible to
     * intercept via an override of beforeRefreshingDatabase() in this base
     * class. refreshApplication() is defined only in the framework TestCase
     * (not in any test trait), so an override here reliably wins.
     */
    protected function refreshApplication(): void
    {
        parent::refreshApplication();

        if (config('database.default') !== 'sqlite') {
            return;
        }

        $this->app['db']->connection()->beforeExecuting(
            static function (string &$query, array &$bindings, $connection): void {
                // R1 — ALTER TABLE on `public_geoscience.*` must be killed
                // BEFORE schema stripping. public_geoscience tables are
                // created via raw SQL with PG-only types (TIMESTAMPTZ /
                // JSONB / TEXT[]) so their CREATE was noop'd above; the
                // tables don't exist in sqlite and any ALTER on them
                // would throw "no such table" after schema stripping.
                //
                // Deliberately NOT applied to `silver.*` — those tables
                // are created via Laravel's Schema Builder which produces
                // sqlite-compatible DDL, so they DO exist post-stripping
                // and their ALTERs (e.g., add_dashboard_fields_to_projects
                // adding status/slug) must actually run.
                if (preg_match(
                    '/^\s*ALTER\s+TABLE\s+"?public_geoscience"?\./i',
                    $query,
                )) {
                    $query = 'SELECT 1';

                    return;
                }

                // ── Strip schema prefixes ─────────────────────────────────────────
                $query = str_replace('"silver".', '', $query);
                $query = str_replace('silver.', '', $query);
                $query = str_replace('"public_geoscience".', '', $query);
                $query = str_replace('public_geoscience.', '', $query);
                // Post-2026-05-17 rename: `public_geoscience` → `public_geo`.
                // Strip the new prefix too so ALTERs / SELECTs against the
                // renamed schema reach the bare table in SQLite. The companion
                // sqlite-only sibling 2026_04_14_115000_provision_public_geo_
                // sources_for_test_db.php creates the base `sources` table so
                // the subsequent ADD COLUMN survives migrate.
                $query = str_replace('"public_geo".', '', $query);
                $query = str_replace('public_geo.', '', $query);
                $query = str_replace('"public".', '', $query);

                // ── No-op entire statements that are PostgreSQL-only ──────────────
                $noOpPatterns = [
                    // DDL: schema management
                    '/^\s*CREATE\s+SCHEMA\b/i',
                    '/^\s*DROP\s+SCHEMA\b/i',

                    // DDL: PostgreSQL functions, triggers, sequences and anonymous blocks
                    // (PL/pgSQL, plpython, etc. — not supported by SQLite)
                    '/^\s*CREATE\s+(OR\s+REPLACE\s+)?FUNCTION\b/i',
                    '/^\s*DROP\s+FUNCTION\b/i',
                    '/^\s*CREATE\s+(OR\s+REPLACE\s+)?PROCEDURE\b/i',
                    '/^\s*DROP\s+PROCEDURE\b/i',
                    '/^\s*CREATE\s+(CONSTRAINT\s+)?TRIGGER\b/i',
                    '/^\s*DROP\s+TRIGGER\b/i',
                    '/^\s*CREATE\s+SEQUENCE\b/i',
                    '/^\s*DROP\s+SEQUENCE\b/i',
                    '/^\s*ALTER\s+SEQUENCE\b/i',
                    // Anonymous DO $$ ... $$ blocks (PG-specific procedural language)
                    '/^\s*DO\s+\$\$/i',
                    '/^\s*DO\s+\$\w+\$/i',
                    '/^\s*DO\s+LANGUAGE\b/i',

                    // DML: PostgreSQL session/config variable commands
                    '/^\s*SET\s+search_path\b/i',
                    '/^\s*SET\s+LOCAL\b/i',
                    '/^\s*RESET\b/i',

                    // DML: PostgreSQL GRANT / REVOKE / ALTER DEFAULT PRIVILEGES
                    '/^\s*GRANT\b/i',
                    '/^\s*REVOKE\b/i',
                    '/^\s*ALTER\s+DEFAULT\s+PRIVILEGES\b/i',

                    // DDL: PostGIS geometry column helper
                    '/^\s*SELECT\s+AddGeometryColumn\b/i',

                    // DDL: all CREATE INDEX statements — indexes are not needed for
                    // test correctness and many use PG-only syntax or reference tables
                    // that were no-op'd. Drop ALL of them for a clean SQLite run.
                    '/^\s*CREATE\s+(UNIQUE\s+)?INDEX\b/i',

                    // DDL: ALTER TABLE with PostgreSQL array column types
                    '/^\s*ALTER\s+TABLE\b[^;]*\bADD\s+COLUMN\b[^;]*\[\]/is',

                    // DDL: ALTER TABLE with multiple ADD COLUMN clauses in one
                    // statement (PG supports comma-separated; SQLite does not).
                    '/^\s*ALTER\s+TABLE\b[^;]*\bADD\s+COLUMN\b[^;]*,\s*ADD\s+COLUMN\b/is',

                    // DDL: ALTER TABLE ADD COLUMN with FK REFERENCES clause
                    // (SQLite has limited FK support and can't add FK columns via ALTER)
                    '/^\s*ALTER\s+TABLE\b[^;]*\bADD\s+COLUMN\b[^;]*\bREFERENCES\b/is',

                    // DDL: ALTER TABLE ADD COLUMN containing PostgreSQL-only column types
                    // (JSONB, TIMESTAMPTZ, TEXT[], geometry).  These target tables were
                    // created via raw SQL CREATE TABLE statements that were already no-op'd
                    // above, so the table does not exist in SQLite and the ALTER would fail
                    // with "no such table" or "syntax error".  No-op the ALTER before the
                    // JSONB→text replacement runs so SQLite never sees it.
                    // geometry() is a PostGIS type — also no-op'd here.
                    '/^\s*ALTER\s+TABLE\b[^;]*\bADD\s+COLUMN\b[^;]*\b(JSONB|TIMESTAMPTZ|TEXT\[\]|geometry\s*\()/is',

                    // DDL: ALTER TABLE on tables that were created via raw SQL CREATE TABLE
                    // statements (which were no-op'd above because they contain PG-only
                    // types like TIMESTAMPTZ/JSONB/geometry). These tables do not exist in
                    // SQLite so any ALTER against them fails with "no such table". No-op ALL
                    // ALTER TABLE statements that target these known raw-SQL-only tables.
                    // Covers ADD COLUMN, DROP COLUMN, and any other ALTER form.
                    // (Module 6: rejection_reason TEXT on answer_runs, Module 8: geom
                    // geometry on geochemistry.)
                    '/^\s*ALTER\s+TABLE\b[^;]*\b(answer_runs|answer_retrieval_items|answer_citation_items|answer_citation_spans|document_revisions|document_passages|message_feedback|drill_traces|evidence_items|geochemistry)\b/is',

                    // DML: UPDATE statements on raw-SQL-only tables (tables that were
                    // created via raw SQL and no-op'd above — they don't exist in SQLite).
                    // These backfill statements run after the ALTER TABLEs; without the
                    // table they fail with "no such table". Detect by the table name
                    // appearing as the direct target of UPDATE (handles both
                    // `UPDATE silver.geochemistry g SET ...` and plain `UPDATE geochemistry`).
                    '/^\s*UPDATE\s+(?:silver\.)?geochemistry\b/is',

                    // NOTE: `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` used to be
                    // noop'd here, but that broke a common idiom where the next
                    // migration statement UPDATEs the new column (e.g., a
                    // backfill). Instead we strip `IF NOT EXISTS` below via
                    // str_replace so SQLite accepts the plain `ADD COLUMN`.

                    // DDL: ALTER TABLE ADD CONSTRAINT (SQLite does not support
                    // adding named constraints after table creation)
                    '/^\s*ALTER\s+TABLE\b[^;]*\bADD\s+CONSTRAINT\b/is',

                    // DDL: ALTER TABLE DROP CONSTRAINT
                    '/^\s*ALTER\s+TABLE\b[^;]*\bDROP\s+CONSTRAINT\b/is',

                    // DDL: ALTER TABLE with PostgreSQL-specific COLUMN operations
                    '/^\s*ALTER\s+TABLE\b[^;]*\bALTER\s+COLUMN\b[^;]*\bSET\s+NOT\s+NULL\b/is',
                    '/^\s*ALTER\s+TABLE\b[^;]*\bALTER\s+COLUMN\b[^;]*\bSET\s+DEFAULT\b/is',
                    '/^\s*ALTER\s+TABLE\b[^;]*\bALTER\s+COLUMN\b[^;]*\bSET\s+STATISTICS\b/is',
                    '/^\s*ALTER\s+TABLE\b[^;]*\bALTER\s+COLUMN\b[^;]*\bTYPE\b/is',

                    // DDL: ANALYZE — PostgreSQL planner statistics refresh.
                    '/^\s*ANALYZE\b/i',

                    // R1 — ALTER TABLE ... RENAME COLUMN. SQLite supports the
                    // syntax from 3.25, BUT the table may have been noop'd
                    // earlier (TIMESTAMPTZ/JSONB/TEXT[] in the raw CREATE
                    // TABLE triggered the CREATE TABLE noop). Renaming a
                    // column on a table that doesn't exist is itself a
                    // PostgreSQL-only integration concern.
                    '/^\s*ALTER\s+TABLE\b[^;]*\bRENAME\s+COLUMN\b/is',

                    // R1 — ALTER INDEX ... RENAME TO. Not supported by
                    // SQLite at all, and like RENAME COLUMN, pairs with a
                    // CREATE INDEX that was already noop'd above.
                    '/^\s*ALTER\s+INDEX\b[^;]*\bRENAME\s+TO\b/is',

                    // DDL: Row Level Security
                    '/^\s*ALTER\s+TABLE\b.*\b(ENABLE|DISABLE|FORCE|NO\s+FORCE)\s+ROW\s+LEVEL\s+SECURITY\b/i',
                    '/^\s*CREATE\s+POLICY\b/i',
                    '/^\s*DROP\s+POLICY\b/i',

                    // DDL: Materialized views
                    '/^\s*CREATE\s+MATERIALIZED\s+VIEW\b/i',
                    '/^\s*DROP\s+MATERIALIZED\s+VIEW\b/i',
                    '/^\s*REFRESH\s+MATERIALIZED\s+VIEW\b/i',

                    // DDL: raw CREATE VIEW (may contain PG-specific expressions)
                    '/^\s*CREATE\s+(OR\s+REPLACE\s+)?VIEW\b/i',
                    '/^\s*DROP\s+VIEW\b/i',

                    // DDL: PostgreSQL storage parameter tuning
                    '/^\s*ALTER\s+TABLE\b.*\bSET\s*\(\s*(autovacuum|fillfactor)/i',

                    // DDL: raw CREATE TABLE containing PostgreSQL-only keywords.
                    // These use TIMESTAMPTZ, TIMESTAMP(n) WITHOUT TIME ZONE,
                    // DOUBLE PRECISION, UUID PRIMARY KEY, TEXT[], JSONB, BIGINT
                    // with CONSTRAINT ... PRIMARY KEY syntax, etc. in raw SQL
                    // (not Schema Builder).
                    // IMPORTANT: match JSONB as a column *type* (space before it),
                    // NOT as a type cast (::jsonb in a DEFAULT value — the Schema
                    // Builder emits those and we patch them via str_replace below).
                    // NOTE: [^;]* does NOT match newlines in PHP — use [\s\S]*? instead
                    // so multi-line raw SQL CREATE TABLE bodies are scanned correctly.
                    '/^\s*CREATE\s+(TABLE|TABLE\s+IF\s+NOT\s+EXISTS)\b[\s\S]*?\b(TIMESTAMPTZ|WITHOUT\s+TIME\s+ZONE|\bTEXT\[\]|\s+JSONB\s)/i',

                    // DDL: COMMENT ON
                    '/^\s*COMMENT\s+ON\s+/i',

                    // DML: INSERT ... ON CONFLICT (column_list) DO NOTHING/UPDATE
                    // SQLite supports ON CONFLICT without a column list (as a
                    // table-level clause) but not the column-list form used by PG
                    // (INSERT ... ON CONFLICT (col) DO ...). No-op these entirely.
                    '/^\s*INSERT\b.*\bON\s+CONFLICT\s*\(\s*\w/is',

                    // DML: UPDATE ... SET workspace_id — backfill that depends on the
                    // workspace_id column being present. Since the ADD COLUMN for
                    // workspace_id is noop'd above (FK syntax), this UPDATE would
                    // fail with "no such column". Noop the backfill too.
                    '/^\s*UPDATE\b[^;]*\bSET\s+workspace_id\s*=/is',

                    // DML: PostgreSQL-specific UPDATE/DML using PG-only functions
                    '/^\s*UPDATE\b.*\bREGEXP_REPLACE\b/is',
                    '/^\s*UPDATE\b.*\bRTRIM\s*\(\s*\w+\s*,/is',

                    // DDL: DROP INDEX with or without IF EXISTS
                    '/^\s*DROP\s+INDEX\b/i',
                ];

                foreach ($noOpPatterns as $pattern) {
                    if (preg_match($pattern, $query)) {
                        $query = 'SELECT 1';

                        return;
                    }
                }

                // ── Fix PostgreSQL-specific expressions in Schema Builder output ──
                // These are in DDL generated by the Schema Builder (not raw SQL),
                // so we cannot no-op the entire statement; instead we patch them.

                // Strip PostgreSQL type casts (::jsonb, ::json, ::text, ::uuid, etc.)
                $query = preg_replace("/'::\w+/", "'", $query);

                // Replace gen_random_uuid() with a SQLite-compatible NULL default.
                // Tests never rely on auto-generated UUIDs from DB defaults.
                $query = str_replace('gen_random_uuid()', 'NULL', $query);

                // Replace jsonb/json column type with text (SQLite stores as text)
                $query = preg_replace('/\bjsonb\b/i', 'text', $query);

                // ── Strip CASCADE from DROP TABLE / DROP VIEW ──────────────────
                $query = preg_replace('/\s+CASCADE\s*$/i', '', $query);

                // R1 — Strip `IF NOT EXISTS` from ALTER TABLE ADD COLUMN and
                // `IF EXISTS` from ALTER TABLE DROP COLUMN. SQLite accepts
                // the plain forms; with the clause stripped, the column is
                // actually added/removed so the next migration statement
                // (typically a backfill UPDATE) can succeed.
                $query = preg_replace(
                    '/(ALTER\s+TABLE\s+\S+\s+ADD\s+COLUMN)\s+IF\s+NOT\s+EXISTS\b/i',
                    '$1',
                    $query,
                );
                $query = preg_replace(
                    '/(ALTER\s+TABLE\s+\S+\s+DROP\s+COLUMN)\s+IF\s+EXISTS\b/i',
                    '$1',
                    $query,
                );
            },
        );
    }
}
