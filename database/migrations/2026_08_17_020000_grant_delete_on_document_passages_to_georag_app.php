<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Grant DELETE on silver.document_passages to georag_app.
 *
 * The 2026-08-14 stale-passage GC (ingest_pdf.py::_persist_body, "GC
 * passages superseded by this re-parse") added a
 * `DELETE FROM silver.document_passages ... RETURNING` statement, but
 * georag_app — the role the FastAPI/Hatchet worker connects as — only
 * ever had SELECT/INSERT/UPDATE on this table (confirmed live via
 * information_schema.role_table_grants). Every ingest that reached that
 * GC step failed with `InsufficientPrivilegeError: permission denied for
 * table document_passages`, wiping the whole persist step and stamping
 * silver.ingest_progress as failed — this was the actual cause behind
 * the "100% of failures cluster at the persist stage" pattern seen
 * 2026-08-10 through 2026-08-16 (7 failures / 7 days).
 *
 * Same fix shape as
 * 2026_05_18_120000_grant_delete_on_derive_tables_to_georag_app (missing
 * DELETE grant on a table a later feature started deleting from). Safe:
 * RLS is already enabled + forced on this table (workspace_id-scoped),
 * and the DELETE statement itself is scoped to one report_id, revision 1,
 * chunk_kind='narrative', and only text_hashes NOT produced by the
 * current re-parse — so this grant doesn't widen what a request can
 * delete beyond what the RLS policy + that WHERE clause already permit.
 *
 * Skipped under SQLite (test DB has no Postgres roles).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('GRANT DELETE ON silver.document_passages TO georag_app');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('REVOKE DELETE ON silver.document_passages FROM georag_app');
    }
};
