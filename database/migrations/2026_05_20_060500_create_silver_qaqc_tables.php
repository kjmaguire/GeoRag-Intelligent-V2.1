<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Drillhole schema — silver QA/QC layer.
 *
 *   silver.sample_intervals    — what got sent for assay (drill core /
 *                                channel / chip samples)
 *   silver.sample_dispatches   — batched shipments to the assay lab
 *   silver.qaqc_results        — standards, blanks, duplicates,
 *                                umpire samples — with pass/fail logic
 *                                as a STORED generated column.
 *
 * The pass/fail generated column codifies the standard QA/QC rules:
 *   - blanks fail when reported_value > 3× expected (typical detection-
 *     limit multiplier convention)
 *   - certified-reference standards fail when |reported - expected| /
 *     expected × 100 > tolerance_pct (10% default per OREAS / CCRMP
 *     recommendation)
 *   - everything else passes (duplicates / umpire have separate flows)
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

        // ── silver.sample_dispatches ──────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.sample_dispatches (
                id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id      uuid NOT NULL,
                dispatch_ref      text NOT NULL,
                lab_name          text NOT NULL,
                dispatched_date   date,
                received_date     date,
                sample_count      integer,
                rush              boolean DEFAULT false,
                analysis_package  text,
                notes             text,
                created_at        timestamptz NOT NULL DEFAULT now()
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS silver_sample_dispatches_workspace_idx ON silver.sample_dispatches (workspace_id)');

        // ── silver.sample_intervals ───────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.sample_intervals (
                id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id      uuid NOT NULL,
                collar_id         uuid NOT NULL REFERENCES silver.collars(collar_id),
                sample_id         text NOT NULL,
                from_depth        numeric NOT NULL,
                to_depth          numeric NOT NULL,
                sample_type       text NOT NULL,
                sample_weight_kg  numeric,
                dispatch_id       uuid REFERENCES silver.sample_dispatches(id),
                submitted_at      date,
                created_at        timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT silver_sample_intervals_valid_interval CHECK (to_depth > from_depth),
                UNIQUE (workspace_id, sample_id)
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS silver_sample_intervals_workspace_collar_idx ON silver.sample_intervals (workspace_id, collar_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS silver_sample_intervals_workspace_id_idx ON silver.sample_intervals (workspace_id)');

        // ── silver.qaqc_results ───────────────────────────────────────────
        // pass_fail is a STORED GENERATED column so the per-row rule is
        // applied at INSERT time and indexable.
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.qaqc_results (
                id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id      uuid NOT NULL,
                dispatch_id       uuid REFERENCES silver.sample_dispatches(id),
                sample_id         text NOT NULL,
                qaqc_type         text NOT NULL,
                standard_ref      text,
                element           text NOT NULL,
                expected_value    numeric,
                reported_value    numeric,
                unit              text NOT NULL,
                tolerance_pct     numeric DEFAULT 10,
                pass_fail         text GENERATED ALWAYS AS (
                    CASE
                      WHEN qaqc_type = 'blank'
                        AND expected_value IS NOT NULL
                        AND reported_value > expected_value * 3
                        THEN 'fail'
                      WHEN qaqc_type IN ('certified_reference', 'standard')
                        AND expected_value IS NOT NULL
                        AND expected_value <> 0
                        AND abs(reported_value - expected_value) / expected_value * 100 > tolerance_pct
                        THEN 'fail'
                      ELSE 'pass'
                    END
                ) STORED,
                bronze_source_id  uuid REFERENCES bronze.raw_qaqc_submissions(id),
                created_at        timestamptz NOT NULL DEFAULT now()
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS silver_qaqc_results_workspace_type_pf_idx ON silver.qaqc_results (workspace_id, qaqc_type, pass_fail)');
        DB::statement('CREATE INDEX IF NOT EXISTS silver_qaqc_results_workspace_id_idx ON silver.qaqc_results (workspace_id)');

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.qaqc_results.pass_fail IS
              'STORED generated column applying the standard QA/QC pass rules: blanks > 3× expected fail; certified-reference samples with |Δ| / expected × 100 > tolerance_pct fail. Duplicates / umpire always pass via this rule — they have separate Δ-tracking in gold.qaqc_statistics.'
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS silver.qaqc_results');
        DB::statement('DROP TABLE IF EXISTS silver.sample_intervals');
        DB::statement('DROP TABLE IF EXISTS silver.sample_dispatches');
    }
};
