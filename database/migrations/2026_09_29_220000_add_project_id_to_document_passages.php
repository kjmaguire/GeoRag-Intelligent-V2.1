<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * silver.document_passages.project_id for synthesized passages (audit
 * RAG-9, 2026-09-29).
 *
 * A passage built from a report gets its project through
 * silver.reports.project_id. A passage SYNTHESIZED from structured rows —
 * the ADR-0012 `structured_summary` chunks nl_summaries writes, one per
 * collar / assay interval / lithology interval — has document_id NULL, so
 * passage_embedder wrote its Qdrant point with project_id NULL. The
 * `project_or_public` retrieval scope admits an empty project_id (that
 * branch exists for public-geoscience chunks), so project B's question
 * retrieved project A's per-hole assay summaries. Tenant isolation held;
 * project isolation did not.
 *
 * The column is nullable: report-derived passages keep taking the project
 * from their report (the embedder COALESCEs the two), and public-geoscience
 * passages have no project by design. nl_summaries fills it from the
 * source collar; re-running nl_summaries NULLs embedding_id wherever the
 * project changed, so the embed sweep rewrites those points with it.
 *
 * ON DELETE CASCADE matches the passage's other parent (a deleted project's
 * synthesized summaries describe rows that no longer exist). Row-level
 * security on this table is by workspace_id and is unchanged by a new
 * column.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE silver.document_passages
              ADD COLUMN IF NOT EXISTS project_id uuid NULL
                REFERENCES silver.projects(project_id) ON DELETE CASCADE
        SQL);

        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_document_passages_project_id
                ON silver.document_passages (project_id)
             WHERE project_id IS NOT NULL
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.document_passages.project_id IS
              'Project of a passage with no parent report (synthesized structured_summary chunks, written by nl_summaries from the source collar). NULL for report-derived passages, which take their project from silver.reports, and for public-geoscience passages. passage_embedder uses COALESCE(reports.project_id, document_passages.project_id) for the Qdrant payload.'
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP INDEX IF EXISTS silver.idx_document_passages_project_id');
        DB::statement('ALTER TABLE silver.document_passages DROP COLUMN IF EXISTS project_id');
    }
};
