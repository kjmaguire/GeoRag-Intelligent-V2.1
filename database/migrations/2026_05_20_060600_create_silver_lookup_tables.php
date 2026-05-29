<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Drillhole schema — silver lookup tables.
 *
 *   silver.rock_codes        — workspace-scoped standardised rock codes
 *                              used by silver.lithology.rock_code.
 *   silver.element_reference — GLOBAL reference data (no workspace_id;
 *                              chemistry is the same everywhere). Used
 *                              by the bronze→silver assay transform to
 *                              convert reported units to ppm.
 *
 * The element_reference table is seeded with the 12 elements Kyle
 * flagged as most-common in the prompt's Step 11 questions. The
 * schema permits adding more without a migration; the seed below
 * is the starting point, not the complete catalogue.
 *
 * SQLite (test DB) — gated on Postgres.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // ── silver.rock_codes ─────────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.rock_codes (
                id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id    uuid NOT NULL,
                code            text NOT NULL,
                name            text NOT NULL,
                description     text,
                UNIQUE (workspace_id, code)
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS silver_rock_codes_workspace_idx ON silver.rock_codes (workspace_id)');

        // ── silver.element_reference (GLOBAL — no workspace_id) ───────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.element_reference (
                symbol                   text PRIMARY KEY,
                name                     text NOT NULL,
                atomic_number            integer,
                default_unit             text NOT NULL,
                ppm_conversion           numeric NOT NULL DEFAULT 1,
                detection_limit_typical  numeric
            )
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON TABLE silver.element_reference IS
              'GLOBAL geochemistry reference data — chemistry is the same regardless of tenant, so this table is intentionally NOT workspace-scoped. Queries against it are always joined through a workspace-scoped fact table (silver.assays_v2, silver.qaqc_results) so RLS still applies at the call site.'
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.element_reference.ppm_conversion IS
              'Multiply (value, default_unit) by this number to get parts-per-million. E.g. Au at ppb → 0.001; Cu at pct → 10000.'
        SQL);

        // ── Seed element_reference with the 12 most-common elements ──────
        // ON CONFLICT DO NOTHING so the migration is re-runnable.
        DB::statement(<<<'SQL'
            INSERT INTO silver.element_reference
              (symbol, name, atomic_number, default_unit, ppm_conversion, detection_limit_typical)
            VALUES
              ('Au', 'Gold',       79, 'ppb', 0.001,  0.5),
              ('Ag', 'Silver',     47, 'ppm', 1.0,    0.2),
              ('Cu', 'Copper',     29, 'pct', 10000,  0.001),
              ('Pb', 'Lead',       82, 'pct', 10000,  0.001),
              ('Zn', 'Zinc',       30, 'pct', 10000,  0.001),
              ('Ni', 'Nickel',     28, 'ppm', 1.0,    0.1),
              ('Co', 'Cobalt',     27, 'ppm', 1.0,    0.1),
              ('Mo', 'Molybdenum', 42, 'ppm', 1.0,    0.05),
              ('U',  'Uranium',    92, 'ppm', 1.0,    0.1),
              ('As', 'Arsenic',    33, 'ppm', 1.0,    0.5),
              ('Sb', 'Antimony',   51, 'ppm', 1.0,    0.1),
              ('Bi', 'Bismuth',    83, 'ppm', 1.0,    0.1)
            ON CONFLICT (symbol) DO NOTHING
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS silver.element_reference');
        DB::statement('DROP TABLE IF EXISTS silver.rock_codes');
    }
};
