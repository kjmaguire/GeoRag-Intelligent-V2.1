<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Make bronze.provenance's COMMENT describe what it actually covers.
 *
 * DM-11 reported that "the PDF ingest path writes no bronze.provenance
 * rows". Measured across every module that writes a silver row from an
 * ingested file, the true picture is wider and more useful:
 *
 *   writes bronze.provenance   cameco_log_ingester, derive_intervals,
 *                              las_ingester, public_geoscience_pull
 *                              (csv_collar_ingester also does, and has
 *                              zero callers)
 *
 *   carries lineage INLINE     ingest_spatial     -> source_file,
 *   on the silver row instead                        source_file_sha256,
 *                                                    source_layer,
 *                                                    source_feature_id
 *                              ingest_well_logs   -> source_file,
 *                                                    las_version
 *                              ingest_pdf         -> source_object_key,
 *                                                    source_file_sha256,
 *                                                    parser_used
 *                                                    + audit.audit_ledger
 *
 *   no lineage by any means    ingest_tabular     -> silver.collars,
 *                                                    surveys,
 *                                                    lithology_logs,
 *                                                    samples
 *
 * Two things follow, and the old COMMENT hid both.
 *
 * First, this table's schema is TABULAR. `source_row INTEGER` and
 * `source_col_map JSONB` describe a cell in a spreadsheet. They are
 * meaningless for a 400-page NI 43-101, and a provenance row per extracted
 * passage would be thousands of rows carrying one file's identity plus two
 * permanent NULLs -- all of it already derivable from
 * silver.document_passages.document_id -> silver.reports. So the PDF path
 * is not a gap in this table; it is out of its scope, and audit.audit_ledger
 * is where its lineage lives. Two UI regressions (SourcesController
 * 2026-08-17, ReportController 2026-08-18) came from joining through here
 * for document data; both now read silver directly.
 *
 * Second, ingest_tabular IS a gap, and the old wording made it invisible by
 * claiming the table already mapped "each silver record". The same
 * silver.collars row gets a provenance row when its file arrives inside a
 * ZIP (cameco_log_ingester) and none when the same file is uploaded
 * directly. That is recorded in the COMMENT rather than quietly fixed here,
 * because closing it means adding RETURNING to a batched executemany on the
 * hot ingest path -- a behaviour change that wants a live database to verify
 * against, not a comment migration.
 *
 * Comment-only. No data, no structure, no constraint is touched.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement(<<<'SQL'
            COMMENT ON TABLE bronze.provenance IS
            'Immutable audit trail for TABULAR ingest: maps a silver record to its source file, row, parser and ingest run. Written by cameco_log_ingester, derive_intervals, las_ingester and public_geoscience_pull. NOT the lineage store for document-derived rows (silver.reports / silver.document_passages) -- source_row and source_col_map have no meaning for a PDF, and that path records lineage on silver.reports (source_object_key, source_file_sha256, parser_used) plus audit.audit_ledger. Spatial and well-log rows carry their lineage inline on the silver row (source_file, source_file_sha256, source_layer, source_feature_id). KNOWN GAP as of 2026-08-21: ingest_tabular writes silver.collars/surveys/lithology_logs/samples with no provenance row and no inline source columns, so a drill file uploaded directly has no lineage while the same file inside a ZIP does.'
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN bronze.provenance.source_row IS
            'Row number in the source spreadsheet or CSV. NULL for any source that is not row-oriented -- which is why this table does not cover the document ingest path.'
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN bronze.provenance.source_col_map IS
            'Source column header -> silver column, as resolved by the parser. Tabular only, for the same reason as source_row.'
        SQL);
    }

    public function down(): void
    {
        DB::statement(<<<'SQL'
            COMMENT ON TABLE bronze.provenance IS
            'Immutable audit trail: maps each silver record to its source file, row, parser, and ingest run. Supports compliance reporting and deduplication.'
        SQL);

        DB::statement('COMMENT ON COLUMN bronze.provenance.source_row IS NULL');
        DB::statement('COMMENT ON COLUMN bronze.provenance.source_col_map IS NULL');
    }
};
