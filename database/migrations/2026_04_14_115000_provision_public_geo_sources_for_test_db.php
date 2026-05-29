<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Sqlite-only sibling for the `public_geo.sources` registry table.
 *
 * `2026_04_14_000000_create_public_geoscience_schema.php` creates
 * `public_geo.sources` via raw SQL containing TIMESTAMPTZ columns. The
 * SQLite test bootstrap (tests/TestCase.php) no-ops every raw CREATE TABLE
 * that mentions a PG-only type, so the table is never materialised in the
 * `:memory:` fixture. Subsequent migrations then ALTER `public_geo.sources`
 * — most notably `2026_04_14_120000_add_last_service_edit_to_sources.php`
 * — and the unguarded `ADD COLUMN` blows up `migrate:fresh` with
 * "no such table: public_geo.sources", which in turn fails every Feature
 * test that uses `RefreshDatabase`.
 *
 * Pair with the `public_geo.` schema-prefix stripper added to
 * `tests/TestCase.php::refreshApplication()` (2026-05-23). Together:
 *   • stripper rewrites `public_geo.sources` → `sources` for SQLite
 *   • this migration creates a minimal `sources` mirror with the base
 *     column shape from the Phase-2.3 schema migration
 *
 * `last_service_edit_ms` is *intentionally omitted* — the 2026_04_14_120000
 * ALTER will add it once this table exists. Other later ALTERs on
 * `public_geo.sources` only DROP/ADD the `sources_canonical_type_check`
 * constraint, both of which the SQLite bootstrap already no-ops.
 *
 * Postgres test DB + production: this migration is a no-op. The real
 * schema/grants live in the raw `2026_04_14_000000` migration.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'sqlite') {
            return;
        }

        // After bootstrap stripping, `public_geo.sources` resolves to a
        // bare `sources` table. Mirror the base column shape from
        // 2026_04_14_000000_create_public_geoscience_schema.php using
        // SQLite-friendly types (no TIMESTAMPTZ / no FK reference to the
        // also-noop'd jurisdictions table).
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS public_geo.sources (
                source_id            TEXT     PRIMARY KEY,
                jurisdiction_code    TEXT     NOT NULL,
                name                 TEXT     NOT NULL,
                canonical_type       TEXT     NOT NULL,
                service_url          TEXT     NOT NULL,
                layer_index          INTEGER  NULL,
                source_crs           INTEGER  NULL,
                license_summary      TEXT     NULL,
                license_url          TEXT     NULL,
                refresh_cadence      TEXT     NULL,
                last_refreshed_at    DATETIME NULL,
                notes                TEXT     NULL,
                created_at           DATETIME NULL,
                updated_at           DATETIME NULL
            )
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'sqlite') {
            return;
        }

        DB::statement('DROP TABLE IF EXISTS public_geo.sources');
    }
};
