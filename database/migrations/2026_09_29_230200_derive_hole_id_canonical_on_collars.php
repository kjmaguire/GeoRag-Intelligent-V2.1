<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * §04e: one collar per (project, canonical hole id) — part 1 of 2: make
 * `hole_id_canonical` trustworthy (SME-approved, Kyle, 2026-09-29).
 *
 * The key is only as good as the column. Before this, each writer filled it
 * its own way: ingest_tabular / csv_collar / las computed the canonical form,
 * cameco_log_ingester stored the RAW hole id (`$1, $1`), and the Laravel
 * collar API stored NULL. So `SRE09-6` from a .log and `SRE09_6` from a collar
 * table carried different "canonical" values, slipped past
 * uq_collars_project_hole_canonical (2026_04_18_130100, partial, NOT NULL
 * rows only), and became two collars — the ghost collars ING-14 found.
 *
 * Now the database derives it: `silver.canonical_hole_id(text)` is the SQL
 * twin of georag_geoparsers._hole_id.canonicalize (and App\Support\HoleId),
 * and `trg_collars_hole_id_canonical` applies it on every INSERT and on any
 * UPDATE that touches hole_id or hole_id_canonical. A BEFORE trigger runs
 * before ON CONFLICT arbitration, so an upsert keyed on the canonical column
 * sees the derived value whatever the writer sent.
 *
 * Existing rows are backfilled — and the unique index built — by part 2
 * (2026_09_29_230300_unique_canonical_hole_id_on_collars), which cannot run
 * in a transaction.
 *
 * `UPDATE OF hole_id, hole_id_canonical`, not every UPDATE: a writer that
 * only touches coordinates or depth (cameco's GREATEST(total_depth)) must not
 * rewrite the key of a not-yet-merged ghost and trip the unique index.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.canonical_hole_id(raw text)
            RETURNS text
            LANGUAGE sql
            IMMUTABLE
            PARALLEL SAFE
            AS $fn$
                SELECT NULLIF(
                    upper(regexp_replace(
                        regexp_replace(raw, '^\s+|\s+$', '', 'g'),
                        '[ ._/-]+', '', 'g'
                    )),
                    ''
                )
            $fn$
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON FUNCTION silver.canonical_hole_id(text) IS
            'Canonical join key of a hole id: trim, drop space/-/_/./slash, uppercase, empty -> NULL. Same rule as georag_geoparsers._hole_id.canonicalize and App\Support\HoleId (§04e, 2026-09-29).'
        SQL);

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.collars_set_hole_id_canonical()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $fn$
            BEGIN
                NEW.hole_id_canonical := silver.canonical_hole_id(NEW.hole_id);
                RETURN NEW;
            END
            $fn$
        SQL);

        DB::statement('DROP TRIGGER IF EXISTS trg_collars_hole_id_canonical ON silver.collars');
        DB::statement(<<<'SQL'
            CREATE TRIGGER trg_collars_hole_id_canonical
                BEFORE INSERT OR UPDATE OF hole_id, hole_id_canonical ON silver.collars
                FOR EACH ROW EXECUTE FUNCTION silver.collars_set_hole_id_canonical()
        SQL);

        // The runtime role writes collars, so it runs the trigger's call.
        DB::statement('GRANT EXECUTE ON FUNCTION silver.canonical_hole_id(text) TO georag_app');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP TRIGGER IF EXISTS trg_collars_hole_id_canonical ON silver.collars');
        DB::statement('DROP FUNCTION IF EXISTS silver.collars_set_hole_id_canonical()');
        DB::statement('DROP FUNCTION IF EXISTS silver.canonical_hole_id(text)');
    }
};
