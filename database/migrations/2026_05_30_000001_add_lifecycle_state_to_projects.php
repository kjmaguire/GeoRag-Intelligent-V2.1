<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * CC-03 Item 8 — Project lifecycle state (soft freeze).
 *
 * Adds a `lifecycle_state` ENUM column to silver.projects so projects
 * can be frozen without losing any data.  The four states map onto the
 * decision documented in memory/project_cc03_item8_hibernation_deferred.md:
 *
 *   active      — normal operation (default)
 *   hibernated  — soft freeze: ingest, AI queries, and user access blocked;
 *                 ALL data (PG rows, Qdrant vectors, Neo4j graph, MinIO files)
 *                 remains intact; reactivation is instant with no re-ingest.
 *                 "Best for long-term RAG quality" per Kyle's 2026-05-29 call.
 *   archived    — permanent freeze; same data-preservation contract as
 *                 hibernated but intended for end-of-life projects.
 *   past_due    — payment lapse; access suspended until payment is resolved.
 *                 Billing wiring is intentionally NOT added here (deferred
 *                 per Kyle's pricing decision).
 *
 * RLS note: lifecycle_state is APPLICATION-LAYER access control only.
 * Workspace-scoped RLS policies on silver.projects (which filter by
 * `app.workspace_id`) do NOT need updating — a hibernated project is still
 * visible to the owning workspace; the FastAPI middleware and Hatchet guards
 * enforce the operational block before any data is touched.  A DB-level
 * comment on the column records this decision so it is self-documenting.
 *
 * SQLite (test DB) — gated on Postgres (silver schema not present in SQLite).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // 1. Add the ENUM-backed column with NOT NULL + default 'active'.
        //    The CHECK constraint is the authoritative enforcement layer;
        //    Postgres native ENUM types were avoided to keep the migration
        //    idempotent (no shared type object that can conflict between
        //    parallel test runs or re-runs of this migration).
        DB::statement(<<<'SQL'
            ALTER TABLE silver.projects
              ADD COLUMN IF NOT EXISTS lifecycle_state text
                NOT NULL
                DEFAULT 'active'
                CHECK (lifecycle_state IN ('active', 'hibernated', 'archived', 'past_due'))
        SQL);

        // 2. Column comment — documents the RLS non-impact decision so a
        //    future engineer reviewing silver.projects doesn't assume they
        //    need to add a lifecycle_state filter to the RLS policy.
        //
        //    The comment text is intentionally verbose: lifecycle_state is
        //    APPLICATION-LAYER control only. A hibernated project is still
        //    visible to the owning workspace via RLS; only ingest, AI queries,
        //    and user access are blocked. Do NOT add lifecycle_state to the
        //    RLS USING clause — that would prevent owners from reactivating
        //    their own projects.
        DB::statement(
            'COMMENT ON COLUMN silver.projects.lifecycle_state IS '
            ."'CC-03 Item 8 soft-freeze lifecycle. Values: active (default), hibernated, archived, past_due. "
            .'RLS NOTE: lifecycle_state is enforced at the application layer (FastAPI middleware + Hatchet guards), '
            .'NOT inside the workspace-scoped RLS policy. A non-active project is still visible to the owning workspace; '
            .'operational access (ingest, AI queries, user access) is blocked by app code, not by DB row filtering. '
            ."Do NOT add this column to the RLS USING clause -- that would prevent owners from reactivating their own projects.'",
        );

        // 3. Index for efficient workspace-scoped lifecycle queries:
        //    e.g. "all active projects in workspace X" or
        //    "all hibernated projects in workspace X" for a billing sweep.
        DB::statement(
            'CREATE INDEX IF NOT EXISTS silver_projects_workspace_lifecycle_idx '
            .'ON silver.projects (workspace_id, lifecycle_state)',
        );
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement('DROP INDEX IF EXISTS silver.silver_projects_workspace_lifecycle_idx');
        DB::statement(
            'ALTER TABLE silver.projects DROP COLUMN IF EXISTS lifecycle_state',
        );
    }
};
