<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Third pass on the RLS admin-escape-hatch closure started by
 * 2026_08_14_030000_close_rls_admin_escape_hatch_verified_subset and
 * continued by 2026_08_15_020100_close_rls_admin_escape_hatch_second_pass.
 * Both prior passes deliberately deferred the 8 highest-traffic tables
 * (silver.projects, collars, reports, document_passages, answer_runs,
 * evidence_items, exports, samples) pending a live-DB-verified pass that
 * traces every real Laravel/FastAPI call site, not just static grep.
 *
 * This pass did exactly that — live Postgres access via the
 * `georag-postgresql` dev container (PG18, full phase0-bootstrapped
 * schema) confirmed the CURRENT fail-open policy shape for all 8 tables:
 * exactly ONE permissive policy per table (`<table>_workspace_isolation`),
 * `ENABLE + FORCE ROW LEVEL SECURITY` already on, `workspace_id` already
 * NOT NULL on every one of them, and — unlike the second pass's targeting.*
 * finding — NO duplicate/legacy second policy on any of the 8. So the
 * "flip only, no policy-name games" question is settled; the remaining
 * question per table was purely "does every live reader/writer bind
 * `app.workspace_id` before touching this table."
 *
 * Four parallel call-site audits (one per table pair, covering every
 * controller/service/job/console-command in `app/` and every
 * router/service in `src/fastapi/app/`) found that 7 of the 8 tables have
 * real, non-trivial gaps — often a structural "look up an opaque ID in
 * THIS table to discover its own workspace_id" pattern that a mechanical
 * `withWorkspaceRls()` wrap cannot fix without an API/job-payload
 * redesign (see the per-table notes below). Converting those 7 as-is
 * would not leak data — fail-closed is safe-by-construction — but it
 * would silently zero-row several live, high-traffic features. Per this
 * pass's brief, tables in that state stay fail-open, documented here
 * rather than flipped blind.
 *
 * TABLE CONVERTED THIS PASS (1 of 8) — silver.document_passages
 * ------------------------------------------------------------------
 * The RAG chunk-retrieval table. Its primary read path — Qdrant-backed
 * hybrid search in `src/fastapi/app/agent/tools.py::search_documents` —
 * never queries this table directly (chunk text/metadata come from the
 * Qdrant point payload), so it isn't a factor either way. The one FastAPI
 * SQL reader (`src/fastapi/app/routers/evidence.py::_fetch_passage_with_
 * context`) queries columns (`document_revision_id`, `passage_text`,
 * `page_number`) that do not exist on the live table (real columns:
 * `document_id`, `text`, `page_first`/`page_last`) — it 500s today
 * regardless of RLS state, a pre-existing bug independent of this
 * migration, flagged separately for its own fix. `src/fastapi/app/
 * services/qdrant_fallback.py`'s lexical-search fallback already calls
 * `bind_workspace_scope()` correctly. `src/fastapi/app/agent/
 * spatial_temporal_verify.py` (which also joins this table) is unimported
 * dead code — not a live call site.
 *
 * That left 4 Laravel Foundry controllers with live gaps, all fixed
 * alongside this migration (same fix pattern as PublicApiController::
 * targets() from pass 2 — wrap the query in SetsWorkspaceRlsContext::
 * withWorkspaceRls(), using a workspace_id resolved via silver.projects,
 * which stays fail-open this pass so the anchor lookup is safe regardless
 * of GUC state):
 *   - app/Http/Controllers/Foundry/ReportController.php::view() — the
 *     passage-excerpt query and dataQualityFlagSummary()'s two
 *     document_passages joins.
 *   - app/Http/Controllers/Foundry/SourcesController.php::show() — the
 *     project passage count (the method already resolved $workspaceId
 *     from $project at the top but never used it — now it does).
 *   - app/Http/Controllers/Foundry/IngestionRunsController.php —
 *     threaded workspace_id through buildSnapshot()/loadReports() to
 *     wrap the passage/embed-progress rollup query.
 *   - app/Http/Controllers/Foundry/CorpusController.php::show() — the
 *     passage count and recent-passages queries.
 * `app/Console/Commands/Ingestion/ReingestProject.php` deletes
 * silver.reports directly and relies on document_passages.document_id's
 * `ON DELETE CASCADE` FK to clean up passages — referential-integrity
 * actions bypass row security per PostgreSQL docs, so that path is
 * unaffected either way. `ProjectController::destroy()` doesn't touch
 * document_passages directly for the same reason (cascades via reports).
 *
 * TABLES NOT converted this pass (left fail-open, with reasons)
 * ------------------------------------------------------------------
 *   - silver.projects / silver.collars: the audits found ~50 combined
 *     live call sites. Nearly every one is rooted in
 *     `App\Models\User::hasProjectAccess()` (Laravel) or
 *     `app/services/workspace_resolution.py::_lookup_workspace_for_
 *     project` (FastAPI) — both do the opaque "look up this exact row,
 *     THEN learn its workspace_id" pattern on the very table being
 *     protected, chicken-and-egg by construction. Collars additionally
 *     has zero GUC binding anywhere in `src/fastapi/app/agent/tools.py`
 *     (the live chat/RAG tool file — collar/assay/coverage queries) and
 *     in all three hallucination-prevention validators. Not fixable
 *     within this pass without redesigning the core authorization
 *     primitive; flipping either table today would break most of the
 *     authenticated app, not just attackers.
 *   - silver.reports: ~19 live gaps across Foundry pages and FastAPI
 *     agent tools, PLUS two pre-existing cross-tenant IDOR bugs
 *     independent of RLS (PublicApiController::reports() and
 *     PublicGeoscience\EntityReferencesController have no tenancy gate
 *     at all today) that need their own fix regardless of this
 *     migration. Flagged separately; not attempted here to keep this
 *     migration to RLS-only changes.
 *   - silver.answer_runs: the RAG citation/audit-trail table. The two
 *     ONLY live INSERT paths — `src/fastapi/app/services/answer_run_
 *     store.py::insert_answer_run` (legacy path) and
 *     `src/fastapi/app/agent/agentic_retrieval/nodes.py::_insert_
 *     answer_run_with_retry` (agentic path) — never bind the GUC.
 *     Flipping today would make every answer's citation audit row
 *     silently stop persisting (WITH CHECK rejects workspace_id = NULL)
 *     — a severe, silent regression of the "citations are mandatory"
 *     invariant. Needs a dedicated, carefully-tested fix pass, not a
 *     rider on this migration.
 *   - silver.evidence_items: smaller surface (no live INSERT gap) but
 *     `EvidenceController.php::show()` has the same opaque-ID chicken-
 *     and-egg problem as projects/collars — it queries
 *     `silver.evidence_items` directly to discover the row's
 *     project_id/workspace_id before any workspace context exists. The
 *     FastAPI side (`routers/evidence.py::_fetch_evidence_row`) DOES
 *     already receive workspace_id via the `X-Workspace-Id` header
 *     Laravel sends after its own (unbound) lookup, so it's fixable in
 *     isolation, but the Laravel anchor lookup blocks the table overall.
 *   - silver.exports: small, contained gap surface (ExportController's
 *     5 actions + GenerateExportJob), but two of its paths have the same
 *     structural blocker: `ExportController::download()` is reached via
 *     an opaque `GET /api/v1/exports/{export}/download` route with no
 *     project_id in the URL, and `GenerateExportJob`'s constructor only
 *     receives `$exportId` — both need the export row's own
 *     workspace_id, which under fail-closed can't be looked up without
 *     already knowing it. Also found in passing: `silver.exports.
 *     workspace_id` has no default and no trigger, and `Export::create()`
 *     never sets it (only added to the table via phase0 raw SQL, not the
 *     Laravel migration) — every `ExportController::store()` call would
 *     already throw a NOT NULL violation today, independent of RLS.
 *     Flagged separately; the exports feature needs its own fix pass
 *     (thread workspace_id through the job payload, add it to
 *     Export::$fillable, likely nest the download route under
 *     /projects/{project}/exports/{export}/download) before either the
 *     bug or the RLS flip can land safely.
 *   - silver.samples: 7 Laravel gaps (Collar/CSV/CSA exporters,
 *     CollarController's withCount relations, Foundry OverviewController)
 *     plus a systemic gap in `src/fastapi/app/agent/tools.py` — 4+ live
 *     chat-tool functions (assay data, collar details, numerical-claim
 *     verification, coverage-gap analysis) query silver.samples via a
 *     bare `pg_pool.acquire()` with zero GUC binding anywhere in the
 *     file. Same root cause as collars; needs the same coordinated fix.
 *
 * Regression coverage: tests/Feature/Tenancy/WorkspaceRlsCoverageTest.php
 * ::test_third_pass_document_passages_has_no_fail_open_escape_hatch
 * asserts the one policy converted this pass has no escape-hatch clause.
 */
return new class extends Migration
{
    private const SCHEMA = 'silver';

    private const TABLE = 'document_passages';

    private const POLICY = 'document_passages_workspace_isolation';

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (! $this->tableExists(self::SCHEMA, self::TABLE)) {
            return;
        }

        if (! $this->columnExists(self::SCHEMA, self::TABLE, 'workspace_id')) {
            return;
        }

        $qualified = self::SCHEMA.'.'.self::TABLE;

        // FORCE ROW LEVEL SECURITY so the policy also binds the table
        // owner (georag, via ad-hoc `psql -U georag` sessions outside
        // Hatchet/pgsql_migrations). Idempotent.
        DB::statement("ALTER TABLE {$qualified} ENABLE ROW LEVEL SECURITY");
        DB::statement("ALTER TABLE {$qualified} FORCE ROW LEVEL SECURITY");

        DB::statement('DROP POLICY IF EXISTS '.self::POLICY." ON {$qualified}");
        DB::statement(<<<SQL
            CREATE POLICY {$this->policy()} ON {$qualified}
                USING (
                    workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                )
                WITH CHECK (
                    workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                )
            SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (! $this->tableExists(self::SCHEMA, self::TABLE)) {
            return;
        }
        if (! $this->columnExists(self::SCHEMA, self::TABLE, 'workspace_id')) {
            return;
        }

        $qualified = self::SCHEMA.'.'.self::TABLE;

        // Restore the exact fail-open shape this table had before this
        // migration.
        DB::statement('DROP POLICY IF EXISTS '.self::POLICY." ON {$qualified}");
        DB::statement(<<<SQL
            CREATE POLICY {$this->policy()} ON {$qualified}
                USING (
                    NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                    OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                )
                WITH CHECK (
                    NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                    OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                )
            SQL);
    }

    private function policy(): string
    {
        return self::POLICY;
    }

    private function tableExists(string $schema, string $table): bool
    {
        return DB::table('information_schema.tables')
            ->where('table_schema', $schema)
            ->where('table_name', $table)
            ->exists();
    }

    private function columnExists(string $schema, string $table, string $column): bool
    {
        return DB::table('information_schema.columns')
            ->where('table_schema', $schema)
            ->where('table_name', $table)
            ->where('column_name', $column)
            ->exists();
    }
};
