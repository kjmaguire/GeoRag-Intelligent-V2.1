<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Drillhole schema — Gold tier (aggregated, analysis-ready).
 *
 * 7 materialised aggregate tables that the Dagster silver→gold assets
 * UPSERT into on a schedule. These power the "what's the best
 * intersection?" / "what's the QA/QC pass rate?" queries that
 * geologists actually run, without re-aggregating from silver every
 * time.
 *
 *   gold.assay_composites          — weighted-average grade over intervals
 *   gold.significant_intersections — notable grade intercepts
 *   gold.drill_summaries           — one row per hole
 *   gold.zone_statistics           — grade/thickness per mineralization zone
 *   gold.qaqc_statistics           — pass-rate rollups by lab / element
 *   gold.campaign_summaries        — one row per drilling campaign
 *   gold.element_correlations      — Pearson r between element pairs
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

        // ── gold.assay_composites ─────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS gold.assay_composites (
                id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id      uuid NOT NULL,
                collar_id         uuid NOT NULL REFERENCES silver.collars(collar_id),
                composite_type    text NOT NULL,
                element           text NOT NULL,
                from_depth        numeric NOT NULL,
                to_depth          numeric NOT NULL,
                composite_length  numeric GENERATED ALWAYS AS (to_depth - from_depth) STORED,
                weighted_avg      numeric NOT NULL,
                unit              text NOT NULL,
                cutoff_grade      numeric,
                sample_count      integer,
                min_value         numeric,
                max_value         numeric,
                computed_at       timestamptz NOT NULL DEFAULT now(),
                UNIQUE (workspace_id, collar_id, composite_type, element, from_depth, to_depth)
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS gold_assay_composites_collar_element_idx ON gold.assay_composites (workspace_id, collar_id, element)');
        DB::statement('CREATE INDEX IF NOT EXISTS gold_assay_composites_element_avg_idx ON gold.assay_composites (workspace_id, element, weighted_avg)');
        DB::statement('CREATE INDEX IF NOT EXISTS gold_assay_composites_workspace_id_idx ON gold.assay_composites (workspace_id)');

        // ── gold.significant_intersections ────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS gold.significant_intersections (
                id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id    uuid NOT NULL,
                collar_id       uuid NOT NULL REFERENCES silver.collars(collar_id),
                element         text NOT NULL,
                cutoff_grade    numeric NOT NULL,
                from_depth      numeric NOT NULL,
                to_depth        numeric NOT NULL,
                true_width_m    numeric,
                downhole_length numeric GENERATED ALWAYS AS (to_depth - from_depth) STORED,
                weighted_avg    numeric NOT NULL,
                unit            text NOT NULL,
                peak_value      numeric,
                peak_depth      numeric,
                zone_name       text,
                computed_at     timestamptz NOT NULL DEFAULT now()
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS gold_significant_intersections_workspace_element_idx ON gold.significant_intersections (workspace_id, element)');
        DB::statement('CREATE INDEX IF NOT EXISTS gold_significant_intersections_collar_idx ON gold.significant_intersections (workspace_id, collar_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS gold_significant_intersections_workspace_id_idx ON gold.significant_intersections (workspace_id)');

        // ── gold.drill_summaries ──────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS gold.drill_summaries (
                id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id             uuid NOT NULL,
                collar_id                uuid NOT NULL UNIQUE REFERENCES silver.collars(collar_id),
                hole_id                  text NOT NULL,
                total_depth              numeric,
                assay_coverage_pct       numeric,
                lithology_coverage_pct   numeric,
                recovery_avg_pct         numeric,
                best_au_interval_grade   numeric,
                best_au_interval_from    numeric,
                best_au_interval_to      numeric,
                elements_assayed         text[],
                qaqc_pass_rate           numeric,
                has_geophysics           boolean DEFAULT false,
                computed_at              timestamptz NOT NULL DEFAULT now()
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS gold_drill_summaries_workspace_idx ON gold.drill_summaries (workspace_id)');

        // ── gold.zone_statistics ──────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS gold.zone_statistics (
                id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id    uuid NOT NULL,
                project_id      uuid NOT NULL REFERENCES silver.projects(project_id),
                zone_name       text NOT NULL,
                element         text NOT NULL,
                cutoff_grade    numeric NOT NULL,
                holes_in_zone   integer,
                total_length_m  numeric,
                avg_grade       numeric,
                unit            text NOT NULL,
                max_grade       numeric,
                avg_true_width  numeric,
                grade_thickness numeric GENERATED ALWAYS AS (avg_grade * avg_true_width) STORED,
                computed_at     timestamptz NOT NULL DEFAULT now(),
                UNIQUE (workspace_id, zone_name, element, cutoff_grade)
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS gold_zone_statistics_workspace_project_idx ON gold.zone_statistics (workspace_id, project_id)');

        // ── gold.qaqc_statistics ──────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS gold.qaqc_statistics (
                id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id      uuid NOT NULL,
                period_start      date,
                period_end        date,
                lab_name          text,
                element           text NOT NULL,
                qaqc_type         text NOT NULL,
                samples_submitted integer,
                samples_passed    integer,
                pass_rate_pct     numeric GENERATED ALWAYS AS (
                  samples_passed::numeric / nullif(samples_submitted, 0) * 100
                ) STORED,
                avg_error_pct     numeric,
                computed_at       timestamptz NOT NULL DEFAULT now()
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS gold_qaqc_statistics_workspace_element_idx ON gold.qaqc_statistics (workspace_id, element)');

        // ── gold.campaign_summaries ───────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS gold.campaign_summaries (
                id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id              uuid NOT NULL,
                campaign_id               uuid NOT NULL UNIQUE REFERENCES silver.campaigns(id),
                holes_completed           integer,
                total_metres              numeric,
                avg_hole_depth            numeric,
                elements_assayed          text[],
                best_intersection_grade   numeric,
                best_intersection_element text,
                qaqc_pass_rate            numeric,
                computed_at               timestamptz NOT NULL DEFAULT now()
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS gold_campaign_summaries_workspace_idx ON gold.campaign_summaries (workspace_id)');

        // ── gold.element_correlations ─────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS gold.element_correlations (
                id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id    uuid NOT NULL,
                project_id      uuid REFERENCES silver.projects(project_id),
                element_a       text NOT NULL,
                element_b       text NOT NULL,
                correlation_r   numeric NOT NULL,
                sample_count    integer,
                computed_at     timestamptz NOT NULL DEFAULT now(),
                UNIQUE (workspace_id, project_id, element_a, element_b),
                CONSTRAINT gold_element_correlations_valid_r CHECK (correlation_r BETWEEN -1 AND 1)
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS gold_element_correlations_workspace_project_idx ON gold.element_correlations (workspace_id, project_id)');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS gold.element_correlations');
        DB::statement('DROP TABLE IF EXISTS gold.campaign_summaries');
        DB::statement('DROP TABLE IF EXISTS gold.qaqc_statistics');
        DB::statement('DROP TABLE IF EXISTS gold.zone_statistics');
        DB::statement('DROP TABLE IF EXISTS gold.drill_summaries');
        DB::statement('DROP TABLE IF EXISTS gold.significant_intersections');
        DB::statement('DROP TABLE IF EXISTS gold.assay_composites');
    }
};
