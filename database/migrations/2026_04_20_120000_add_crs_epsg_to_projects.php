<?php

/**
 * Decision C — Add projects.crs_epsg and deprecate projects.crs_datum.
 *
 * Non-destructive: crs_datum column is retained for backward compatibility.
 * The column comment marks it deprecated — Module 10 owns the eventual drop.
 *
 * Module 3 Phase B 2026-04-20. Kyle-approved 2026-04-20.
 *
 * See: ops/audit/2026-04-20-ingestion-audit.md §A4 DSV-04
 *      ops/backlog/module-10-doc-sweep.md "projects.crs_epsg vs projects.crs_datum"
 */

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    public function up(): void
    {
        // Add the new crs_epsg column (INTEGER, nullable — existing rows are unknown)
        DB::statement(
            'ALTER TABLE silver.projects
                ADD COLUMN IF NOT EXISTS crs_epsg INTEGER NULL'
        );

        // Mark crs_datum as deprecated via PostgreSQL column comment.
        // Module 10 will sweep this and drop it once crs_epsg is fully adopted.
        DB::statement(
            "COMMENT ON COLUMN silver.projects.crs_datum IS
             'DEPRECATED 2026-04-20: use crs_epsg (INTEGER) instead.
              crs_datum stores a free-text string (e.g. ''EPSG:32613'') and predates
              the typed crs_epsg column added in Module 3 Phase B.
              Migration to crs_epsg and eventual DROP of this column is Module 10 doc-sweep scope.'"
        );
    }

    public function down(): void
    {
        DB::statement('ALTER TABLE silver.projects DROP COLUMN IF EXISTS crs_epsg');

        // Remove the deprecation comment (restore to no comment)
        DB::statement('COMMENT ON COLUMN silver.projects.crs_datum IS NULL');
    }
};
