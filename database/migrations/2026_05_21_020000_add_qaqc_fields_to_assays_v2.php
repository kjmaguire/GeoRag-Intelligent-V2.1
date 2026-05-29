<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Phase 4 / Step 4.1 — QA/QC fields on silver.assays_v2.
 *
 * Adds the 13 net-new QA/QC columns the geologist-question plan calls for.
 * The remaining 4 fields from the plan's table are already present on
 * assays_v2 under existing names:
 *   - detection_limit  → unchanged
 *   - below_detection  → covered by ``under_detection``
 *   - lab_id           → covered by ``lab_name``
 *   - method_code      → covered by ``analysis_method``
 *
 * The 13 new columns:
 *   - blank_result, blank_threshold, blank_pass — blank-sample QA/QC per batch
 *   - crm_id, crm_expected, crm_result, crm_pass — CRM standard tracking
 *   - duplicate_pair_id, duplicate_rpd, duplicate_pass — field/lab duplicates
 *   - half_dl_substituted — half-detection-limit value substitution flag
 *   - batch_id — laboratory batch identifier
 *   - digestion_code — digestion / sample-prep method
 *
 * All columns are nullable so existing rows are unaffected (additive). The
 * anomaly_detection subgraph (Phase 2) inspects these fields when present
 * and falls back to Silver Review queue metadata when absent.
 *
 * pgTAP-style verification queries: see scripts/phase4_verify_qaqc.sql,
 * applied during the smoke-test step.
 *
 * SQLite (test DB) — gated on Postgres, same pattern as the prior assay
 * schema migrations.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE silver.assays_v2
              ADD COLUMN IF NOT EXISTS blank_result        numeric,
              ADD COLUMN IF NOT EXISTS blank_threshold     numeric,
              ADD COLUMN IF NOT EXISTS blank_pass          boolean,
              ADD COLUMN IF NOT EXISTS crm_id              text,
              ADD COLUMN IF NOT EXISTS crm_expected        numeric,
              ADD COLUMN IF NOT EXISTS crm_result          numeric,
              ADD COLUMN IF NOT EXISTS crm_pass            boolean,
              ADD COLUMN IF NOT EXISTS duplicate_pair_id   text,
              ADD COLUMN IF NOT EXISTS duplicate_rpd       numeric,
              ADD COLUMN IF NOT EXISTS duplicate_pass      boolean,
              ADD COLUMN IF NOT EXISTS half_dl_substituted boolean,
              ADD COLUMN IF NOT EXISTS batch_id            text,
              ADD COLUMN IF NOT EXISTS digestion_code      text
        SQL);

        DB::statement("COMMENT ON COLUMN silver.assays_v2.blank_result IS 'Blank sample assay result for this batch.'");
        DB::statement("COMMENT ON COLUMN silver.assays_v2.blank_threshold IS 'Acceptable blank threshold for this analyte.'");
        DB::statement("COMMENT ON COLUMN silver.assays_v2.blank_pass IS 'NULL = not assessed; TRUE = blank passed; FALSE = blank failed.'");
        DB::statement("COMMENT ON COLUMN silver.assays_v2.crm_id IS 'Certified Reference Material identifier used for this batch.'");
        DB::statement("COMMENT ON COLUMN silver.assays_v2.crm_expected IS 'Expected CRM value (certified mean).'");
        DB::statement("COMMENT ON COLUMN silver.assays_v2.crm_result IS 'Actual CRM measurement returned for this batch.'");
        DB::statement("COMMENT ON COLUMN silver.assays_v2.crm_pass IS 'NULL = not assessed; TRUE = within tolerance; FALSE = CRM failed.'");
        DB::statement("COMMENT ON COLUMN silver.assays_v2.duplicate_pair_id IS 'Links this sample to its field/lab duplicate.'");
        DB::statement("COMMENT ON COLUMN silver.assays_v2.duplicate_rpd IS 'Relative percent difference between sample and duplicate.'");
        DB::statement("COMMENT ON COLUMN silver.assays_v2.duplicate_pass IS 'NULL = not assessed; TRUE = within RPD tolerance; FALSE = failed.'");
        DB::statement("COMMENT ON COLUMN silver.assays_v2.half_dl_substituted IS 'TRUE when reported value is half of the detection limit (under-DL substitution).'");
        DB::statement("COMMENT ON COLUMN silver.assays_v2.batch_id IS 'Laboratory batch identifier (groups samples that share blank/CRM/duplicate QA/QC).'");
        DB::statement("COMMENT ON COLUMN silver.assays_v2.digestion_code IS 'Digestion / sample-prep method code (e.g. 4A = four-acid digestion).'");

        // Index on batch_id — anomaly-detection subgraph groups failed-QA/QC
        // batches; partial index keeps it small (most rows pre-Phase-4 have NULL).
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_assays_v2_batch_id
              ON silver.assays_v2 (batch_id)
              WHERE batch_id IS NOT NULL
        SQL);

        // Index on duplicate_pair_id for fast "find the other half of this
        // duplicate pair" lookups.
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_assays_v2_duplicate_pair_id
              ON silver.assays_v2 (duplicate_pair_id)
              WHERE duplicate_pair_id IS NOT NULL
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement('DROP INDEX IF EXISTS silver.idx_assays_v2_duplicate_pair_id');
        DB::statement('DROP INDEX IF EXISTS silver.idx_assays_v2_batch_id');
        DB::statement(<<<'SQL'
            ALTER TABLE silver.assays_v2
              DROP COLUMN IF EXISTS digestion_code,
              DROP COLUMN IF EXISTS batch_id,
              DROP COLUMN IF EXISTS half_dl_substituted,
              DROP COLUMN IF EXISTS duplicate_pass,
              DROP COLUMN IF EXISTS duplicate_rpd,
              DROP COLUMN IF EXISTS duplicate_pair_id,
              DROP COLUMN IF EXISTS crm_pass,
              DROP COLUMN IF EXISTS crm_result,
              DROP COLUMN IF EXISTS crm_expected,
              DROP COLUMN IF EXISTS crm_id,
              DROP COLUMN IF EXISTS blank_pass,
              DROP COLUMN IF EXISTS blank_threshold,
              DROP COLUMN IF EXISTS blank_result
        SQL);
    }
};
