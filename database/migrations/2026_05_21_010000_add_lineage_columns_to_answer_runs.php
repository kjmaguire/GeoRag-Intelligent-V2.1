<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Phase 1 / Step 1.5 — session-level lineage columns on silver.answer_runs.
 *
 * The §04i provenance contract already records the citation chain for the
 * subset of chunks that were *cited*. The OIUR rollout requires us to also
 * record every chunk that was *considered* (i.e. retrieved + reranked but
 * not necessarily cited) plus the scope/QA-QC filter state at query time,
 * so a second geologist can reconstruct the full evidence path that led to
 * the answer — not just the path the LLM ended up using.
 *
 * Columns introduced
 * ------------------
 *   session_id                  UUID, nullable
 *       Groups successive answers in one chat session. NULL for runs that
 *       arrived without a session header.
 *
 *   lineage_retrieved_sources   JSONB, nullable
 *       Compact list of [{pdf_id, chunk_id, score, source_type}] for every
 *       chunk that entered the LLM context (fused candidates, post-rerank).
 *       Distinct from `silver.answer_retrieval_items` which already stores
 *       the same set in a long table — this column is the snapshot the
 *       audit endpoint returns without an extra join.
 *
 *   lineage_filters_applied     JSONB, nullable
 *       Scope filter state at query time: project, jurisdiction, date
 *       range, data-type selectors. Empty object `{}` means "no scope
 *       narrowing was active".
 *
 *   lineage_qaqc_filters_applied JSONB, nullable
 *       QA/QC exclusions active at query time (e.g. batches gated out by
 *       the Silver Review queue). Empty object means defaults applied.
 *
 *   answer_schema_version       VARCHAR(16), nullable
 *       OIUR schema version that the synthesis step used. NULL when the
 *       OIUR feature flag was off for this run.
 *
 * NULL semantics: all five columns are NULL on pre-rollout rows AND on
 * runs where the OIUR flag is off — the audit endpoint distinguishes by
 * checking `answer_schema_version IS NOT NULL` for "lineage captured".
 *
 * Indexing: session_id gets a partial b-tree (covers replay queries),
 * lineage_retrieved_sources gets a small jsonb_path_ops GIN for the
 * "which answers cited chunk X" audit queries that the §04i contract
 * already drives off `silver.answer_citation_items`. We do NOT index the
 * filter columns — they're returned verbatim and never searched.
 *
 * SQLite (test DB) — gated on Postgres, same pattern as the prior
 * answer_runs migrations.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE silver.answer_runs
              ADD COLUMN IF NOT EXISTS session_id                   uuid,
              ADD COLUMN IF NOT EXISTS lineage_retrieved_sources    jsonb,
              ADD COLUMN IF NOT EXISTS lineage_filters_applied      jsonb,
              ADD COLUMN IF NOT EXISTS lineage_qaqc_filters_applied jsonb,
              ADD COLUMN IF NOT EXISTS answer_schema_version        varchar(16)
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.answer_runs.session_id IS
              'Chat-session UUID grouping successive answers (Phase 1 / Step 1.5).'
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.answer_runs.lineage_retrieved_sources IS
              'Snapshot of every chunk considered for the answer (cited or not). JSONB list of {pdf_id, chunk_id, score, source_type} per chunk.'
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.answer_runs.lineage_filters_applied IS
              'Scope filter state at query time (project, jurisdiction, date range, data type).'
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.answer_runs.lineage_qaqc_filters_applied IS
              'QA/QC exclusions active at query time (e.g. Silver Review batch exclusions).'
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.answer_runs.answer_schema_version IS
              'OIUR schema version used for the answer. NULL = legacy flat-text answer (OIUR flag off).'
        SQL);

        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_answer_runs_session_id
              ON silver.answer_runs (session_id)
              WHERE session_id IS NOT NULL
        SQL);

        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_answer_runs_lineage_sources_gin
              ON silver.answer_runs
              USING gin (lineage_retrieved_sources jsonb_path_ops)
              WHERE lineage_retrieved_sources IS NOT NULL
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement('DROP INDEX IF EXISTS silver.idx_answer_runs_lineage_sources_gin');
        DB::statement('DROP INDEX IF EXISTS silver.idx_answer_runs_session_id');
        DB::statement(<<<'SQL'
            ALTER TABLE silver.answer_runs
              DROP COLUMN IF EXISTS answer_schema_version,
              DROP COLUMN IF EXISTS lineage_qaqc_filters_applied,
              DROP COLUMN IF EXISTS lineage_filters_applied,
              DROP COLUMN IF EXISTS lineage_retrieved_sources,
              DROP COLUMN IF EXISTS session_id
        SQL);
    }
};
