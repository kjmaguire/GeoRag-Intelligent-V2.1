<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * CC-03 Item 1 — fill the last two gaps on silver.assays_v2 for NI 43-101
 * resource estimation (Bahram's blocker).
 *
 * Audit 2026-05-23 vs. the cc-03 spec field list:
 *   lab                  ✅ already exists as `lab_name`
 *   method               ✅ already exists as `analysis_method`
 *   instrument           ❌ MISSING — add as `instrument`
 *   detection_limit      ✅ already exists
 *   certification_status ❌ MISSING — add as `certification_status` enum
 *   qa_qc_flag           ✅ already exists as `qaqc_flag`
 *
 * Net change: two new nullable columns.
 *
 *   instrument            text — analytical instrument identifier
 *                         (e.g. "ICP-MS Agilent 7900", "AA Perkin Elmer
 *                         AAnalyst 800", "XRF Bruker S1 Titan"). Free text
 *                         because lab nomenclature varies wildly across
 *                         vendors; the resource-estimation tooling
 *                         normalises downstream.
 *   certification_status  varchar(16) CHECK enum:
 *                           'certified'    — lab held a current ISO 17025
 *                                            scope for this analyte at the
 *                                            certificate date
 *                           'uncertified'  — lab is operational but no
 *                                            ISO scope for this analyte
 *                           'unknown'      — not recorded on the certificate
 *                                            (most legacy data)
 *
 * Distinct from the existing `certificate_ref` column, which is the
 * laboratory certificate document identifier (e.g. "LIM-25-12345"). The
 * new `certification_status` column captures the QP / NI 43-101 question
 * "was this lab certified for this work?" — a separate fact.
 *
 * SQLite — gated on Postgres, additive only.
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
              ADD COLUMN IF NOT EXISTS instrument            text,
              ADD COLUMN IF NOT EXISTS certification_status  varchar(16)
        SQL);

        // Drop-and-replace pattern so the migration is rerunnable. NOT VALID
        // means historic rows pre-Phase-4 are not retroactively checked;
        // new inserts are.
        DB::statement(<<<'SQL'
            ALTER TABLE silver.assays_v2
              DROP CONSTRAINT IF EXISTS chk_assays_v2_certification_status
        SQL);
        DB::statement(<<<'SQL'
            ALTER TABLE silver.assays_v2
              ADD CONSTRAINT chk_assays_v2_certification_status
              CHECK (certification_status IS NULL
                     OR certification_status IN ('certified', 'uncertified', 'unknown'))
              NOT VALID
        SQL);

        DB::statement("COMMENT ON COLUMN silver.assays_v2.instrument IS
            'CC-03 Item 1 — analytical instrument identifier (vendor + model). Free text; downstream resource-estimation tooling normalises.'");
        DB::statement("COMMENT ON COLUMN silver.assays_v2.certification_status IS
            'CC-03 Item 1 — lab ISO-17025 status for this analyte. certified | uncertified | unknown. Distinct from certificate_ref (the document id).'");

        // Index on (lab_name, certification_status) — resource-estimation
        // QA filter pattern.
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_assays_v2_lab_cert_status
              ON silver.assays_v2 (lab_name, certification_status)
              WHERE certification_status IS NOT NULL
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement('DROP INDEX IF EXISTS silver.idx_assays_v2_lab_cert_status');
        DB::statement('ALTER TABLE silver.assays_v2 DROP CONSTRAINT IF EXISTS chk_assays_v2_certification_status');
        DB::statement(<<<'SQL'
            ALTER TABLE silver.assays_v2
              DROP COLUMN IF EXISTS certification_status,
              DROP COLUMN IF EXISTS instrument
        SQL);
    }
};
