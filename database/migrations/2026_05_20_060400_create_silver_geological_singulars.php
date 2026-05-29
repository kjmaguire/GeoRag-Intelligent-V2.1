<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Drillhole schema — silver geological tables (singular naming per spec).
 *
 * Drops the empty plural pre-existing tables (silver.structures,
 * silver.alterations) and creates the singular spec tables plus the
 * other geological + engineering tables that don't exist yet.
 *
 * The plurals had ZERO rows at audit time (verified 2026-05-20); the
 * drop is safe. silver.geochemistry is also zero-row and unrelated to
 * drillhole geology — left untouched.
 *
 * Creates:
 *   silver.structure           — structural measurements
 *   silver.alteration          — hydrothermal alteration zones
 *   silver.mineralization      — visible ore mineral occurrences
 *   silver.recovery            — core recovery + RQD per run
 *   silver.specific_gravity    — density measurements
 *   silver.geotechnical        — engineering data (UCS, RMR, Q-system)
 *   silver.downhole_geophysics — processed downhole readings
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

        // ── Drop the empty plurals ────────────────────────────────────────
        // Verified zero rows 2026-05-20. If anyone wrote data between
        // audit and this run, this DROP will surface that — psql will
        // refuse to drop a non-empty table referenced by FKs.
        DB::statement('DROP TABLE IF EXISTS silver.structures');
        DB::statement('DROP TABLE IF EXISTS silver.alterations');

        // ── silver.structure ──────────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE silver.structure (
                id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id    uuid NOT NULL,
                collar_id       uuid NOT NULL REFERENCES silver.collars(collar_id),
                depth           numeric NOT NULL,
                structure_type  text NOT NULL,
                alpha_angle     numeric,
                beta_angle      numeric,
                true_dip        numeric,
                true_dip_dir    numeric,
                roughness       text,
                infill          text,
                notes           text,
                created_at      timestamptz NOT NULL DEFAULT now()
            )
        SQL);
        DB::statement('CREATE INDEX silver_structure_workspace_collar_idx ON silver.structure (workspace_id, collar_id)');
        DB::statement('CREATE INDEX silver_structure_workspace_id_idx ON silver.structure (workspace_id)');

        // ── silver.alteration ─────────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE silver.alteration (
                id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id    uuid NOT NULL,
                collar_id       uuid NOT NULL REFERENCES silver.collars(collar_id),
                from_depth      numeric NOT NULL,
                to_depth        numeric NOT NULL,
                alteration_type text NOT NULL,
                intensity       text,
                minerals        text[],
                notes           text,
                created_at      timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT silver_alteration_valid_interval CHECK (to_depth > from_depth)
            )
        SQL);
        DB::statement('CREATE INDEX silver_alteration_workspace_collar_idx ON silver.alteration (workspace_id, collar_id)');
        DB::statement('CREATE INDEX silver_alteration_workspace_id_idx ON silver.alteration (workspace_id)');

        // ── silver.mineralization ─────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE silver.mineralization (
                id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id    uuid NOT NULL,
                collar_id       uuid NOT NULL REFERENCES silver.collars(collar_id),
                from_depth      numeric NOT NULL,
                to_depth        numeric NOT NULL,
                mineral         text NOT NULL,
                abundance_pct   numeric,
                form            text,
                grain_size      text,
                notes           text,
                created_at      timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT silver_mineralization_valid_interval CHECK (to_depth > from_depth),
                CONSTRAINT silver_mineralization_valid_pct CHECK (abundance_pct BETWEEN 0 AND 100 OR abundance_pct IS NULL)
            )
        SQL);
        DB::statement('CREATE INDEX silver_mineralization_workspace_collar_idx ON silver.mineralization (workspace_id, collar_id)');
        DB::statement('CREATE INDEX silver_mineralization_workspace_id_idx ON silver.mineralization (workspace_id)');

        // ── silver.recovery ───────────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE silver.recovery (
                id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id       uuid NOT NULL,
                collar_id          uuid NOT NULL REFERENCES silver.collars(collar_id),
                from_depth         numeric NOT NULL,
                to_depth           numeric NOT NULL,
                core_recovery_pct  numeric,
                rqd_pct            numeric,
                core_diameter      numeric,
                core_size          text,
                run_number         integer,
                notes              text,
                created_at         timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT silver_recovery_valid_pct CHECK (
                    (core_recovery_pct IS NULL OR core_recovery_pct BETWEEN 0 AND 100)
                    AND (rqd_pct IS NULL OR rqd_pct BETWEEN 0 AND 100)
                ),
                CONSTRAINT silver_recovery_valid_interval CHECK (to_depth > from_depth)
            )
        SQL);
        DB::statement('CREATE INDEX silver_recovery_workspace_collar_idx ON silver.recovery (workspace_id, collar_id)');
        DB::statement('CREATE INDEX silver_recovery_workspace_id_idx ON silver.recovery (workspace_id)');

        // ── silver.specific_gravity ───────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE silver.specific_gravity (
                id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id    uuid NOT NULL,
                collar_id       uuid NOT NULL REFERENCES silver.collars(collar_id),
                from_depth      numeric NOT NULL,
                to_depth        numeric NOT NULL,
                sg_value        numeric NOT NULL,
                method          text,
                rock_code       text,
                wet_weight      numeric,
                dry_weight      numeric,
                notes           text,
                created_at      timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT silver_sg_valid_interval CHECK (to_depth > from_depth)
            )
        SQL);
        DB::statement('CREATE INDEX silver_sg_workspace_collar_idx ON silver.specific_gravity (workspace_id, collar_id)');
        DB::statement('CREATE INDEX silver_sg_workspace_id_idx ON silver.specific_gravity (workspace_id)');

        // ── silver.geotechnical ───────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE silver.geotechnical (
                id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id      uuid NOT NULL,
                collar_id         uuid NOT NULL REFERENCES silver.collars(collar_id),
                from_depth        numeric NOT NULL,
                to_depth          numeric NOT NULL,
                rqd_pct           numeric,
                fractures_per_m   numeric,
                joint_sets        integer,
                ucs_mpa           numeric,
                point_load_mpa    numeric,
                rock_mass_rating  numeric,
                q_value           numeric,
                notes             text,
                created_at        timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT silver_geotech_valid_interval CHECK (to_depth > from_depth)
            )
        SQL);
        DB::statement('CREATE INDEX silver_geotech_workspace_collar_idx ON silver.geotechnical (workspace_id, collar_id)');
        DB::statement('CREATE INDEX silver_geotech_workspace_id_idx ON silver.geotechnical (workspace_id)');

        // ── silver.downhole_geophysics ────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE silver.downhole_geophysics (
                id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id    uuid NOT NULL,
                collar_id       uuid NOT NULL REFERENCES silver.collars(collar_id),
                run_id          uuid REFERENCES bronze.raw_geophysical_runs(id),
                depth           numeric NOT NULL,
                reading_type    text NOT NULL,
                value           numeric NOT NULL,
                unit            text NOT NULL,
                created_at      timestamptz NOT NULL DEFAULT now()
            )
        SQL);
        DB::statement('CREATE INDEX silver_dh_geophys_workspace_collar_idx ON silver.downhole_geophysics (workspace_id, collar_id)');
        DB::statement('CREATE INDEX silver_dh_geophys_collar_type_depth_idx ON silver.downhole_geophysics (collar_id, reading_type, depth)');
        DB::statement('CREATE INDEX silver_dh_geophys_workspace_id_idx ON silver.downhole_geophysics (workspace_id)');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS silver.downhole_geophysics');
        DB::statement('DROP TABLE IF EXISTS silver.geotechnical');
        DB::statement('DROP TABLE IF EXISTS silver.specific_gravity');
        DB::statement('DROP TABLE IF EXISTS silver.recovery');
        DB::statement('DROP TABLE IF EXISTS silver.mineralization');
        DB::statement('DROP TABLE IF EXISTS silver.alteration');
        DB::statement('DROP TABLE IF EXISTS silver.structure');
        // We do NOT recreate the dropped plurals in the down() path —
        // they were empty placeholders. If a rollback is needed in
        // production they can be re-created from the original migration.
    }
};
