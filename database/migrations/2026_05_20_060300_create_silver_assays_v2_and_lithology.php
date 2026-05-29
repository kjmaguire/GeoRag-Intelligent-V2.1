<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Drillhole schema — silver.assays_v2 + silver.lithology.
 *
 * Two new tables that intentionally sit ALONGSIDE the existing
 * silver.assays (540 rows, sample-id long-form) and
 * silver.lithology_logs (11,298 rows). The decision (per Kyle
 * 2026-05-20) is to land the spec tables in parallel and migrate
 * downstream code over time, rather than risk the existing rows.
 *
 * Naming:
 *   silver.assays_v2  — drillhole assay intervals (collar_id +
 *                       from_depth + to_depth + element)
 *   silver.lithology  — rock descriptions per drilled interval
 *                       (rock_code + colour + grain_size + …)
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

        // ── silver.assays_v2 ──────────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.assays_v2 (
                id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id     uuid NOT NULL,
                collar_id        uuid NOT NULL REFERENCES silver.collars(collar_id),
                sample_id        text NOT NULL,
                from_depth       numeric NOT NULL,
                to_depth         numeric NOT NULL,
                interval_length  numeric GENERATED ALWAYS AS (to_depth - from_depth) STORED,
                element          text NOT NULL,
                value            numeric,
                unit             text NOT NULL,
                value_ppm        numeric,
                detection_limit  numeric,
                over_detection   boolean DEFAULT false,
                under_detection  boolean DEFAULT false,
                lab_name         text,
                certificate_ref  text,
                analysis_method  text,
                qaqc_flag        text DEFAULT 'pass',
                bronze_source_id uuid REFERENCES bronze.raw_assay_submissions(id),
                created_at       timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT silver_assays_v2_valid_interval CHECK (to_depth > from_depth),
                CONSTRAINT silver_assays_v2_valid_value    CHECK (value >= 0 OR value IS NULL)
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS silver_assays_v2_workspace_collar_idx ON silver.assays_v2 (workspace_id, collar_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS silver_assays_v2_workspace_element_idx ON silver.assays_v2 (workspace_id, element)');
        DB::statement('CREATE INDEX IF NOT EXISTS silver_assays_v2_collar_depth_idx ON silver.assays_v2 (collar_id, from_depth, to_depth)');
        DB::statement('CREATE INDEX IF NOT EXISTS silver_assays_v2_workspace_id_idx ON silver.assays_v2 (workspace_id)');

        DB::statement(<<<'SQL'
            COMMENT ON TABLE silver.assays_v2 IS
              'Drillhole assay intervals (wide-form, one row per from-to-element). New schema landing 2026-05-20; the legacy silver.assays (long-form keyed on sample_id) remains for backward compat. New writes go here.'
        SQL);

        // ── silver.lithology ──────────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.lithology (
                id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id     uuid NOT NULL,
                collar_id        uuid NOT NULL REFERENCES silver.collars(collar_id),
                from_depth       numeric NOT NULL,
                to_depth         numeric NOT NULL,
                rock_code        text,
                rock_name        text,
                description      text,
                colour           text,
                grain_size       text,
                texture          text,
                weathering       text,
                hardness         text,
                logged_by        text,
                logged_date      date,
                bronze_source_id uuid REFERENCES bronze.raw_lithology_logs(id),
                created_at       timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT silver_lithology_valid_interval CHECK (to_depth > from_depth)
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS silver_lithology_workspace_collar_idx ON silver.lithology (workspace_id, collar_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS silver_lithology_collar_depth_idx ON silver.lithology (collar_id, from_depth, to_depth)');
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS silver_lithology_desc_fts_idx
              ON silver.lithology USING gin(to_tsvector('english', description))
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS silver_lithology_workspace_id_idx ON silver.lithology (workspace_id)');

        DB::statement(<<<'SQL'
            COMMENT ON TABLE silver.lithology IS
              'Standardised rock descriptions per drilled interval. New schema landing 2026-05-20; the legacy silver.lithology_logs remains for backward compat. New writes go here.'
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS silver.lithology');
        DB::statement('DROP TABLE IF EXISTS silver.assays_v2');
    }
};
