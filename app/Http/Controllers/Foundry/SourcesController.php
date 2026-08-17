<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Support\SetsWorkspaceRlsContext;
use Illuminate\Http\Request;
use Illuminate\Support\Collection;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/SourcesController — the "Data" surface inside a project.
 *
 * **Project-scoped.**
 *
 * 2026-08-17 — every panel here used to be driven by `bronze.provenance`
 * / `bronze.ingest_manifest` / `bronze.ingest_runs`, keyed off a PLSS
 * section token (`028N079W36`) parsed out of `bronze.provenance.
 * source_file`. That whole path was built for the one-time bulk Wyoming
 * uranium archive import. The live, and now only, ingestion path —
 * direct PDF/TIFF upload through `ingest_pdf.py` / `tiff_ocr_ingester.py`
 * — never writes a `bronze.provenance` row at all, so for every project
 * created through the actual product (not that historical bulk import)
 * `resolveProjectSections()` always returned an empty list and every
 * bronze-keyed panel (file inventory, ingestion runs, parser activity)
 * rendered empty — even though ingestion had fully succeeded and the
 * report was live in chat. Confirmed live: reports existed with real
 * `parser_used`/`parse_quality_pct`, but this page showed 0 for
 * everything except the reports list itself.
 *
 * `CorpusController` already hit this exact bug for its passage count
 * and was fixed to join `document_passages -> reports -> project_id`
 * directly instead of through `bronze.provenance` (see its docblock).
 * This is the same fix applied here, plus repointing the ingestion-runs
 * and parser-activity panels at the tables the live pipeline actually
 * writes: `silver.ingest_progress` (per-file Hatchet run history, same
 * source `IngestionRunsController` already uses) and `silver.reports`
 * (parser_used/parse_quality_pct are written by every real ingest).
 *
 * `resolveProjectSections()` / the bronze `file_types` breakdown stay in
 * place for their original purpose — historical archive-import projects
 * that DO have `bronze.provenance` rows still get a real PLSS-section
 * breakdown — but nothing on this page depends on that being non-empty
 * anymore.
 */
class SourcesController extends Controller
{
    use SetsWorkspaceRlsContext;

    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()
            ->where('silver.projects.project_id', $project->project_id)
            ->firstOrFail();

        $workspaceId = $project->workspace_id;

        // ── 0. PLSS sections, for legacy bulk-archive projects only. ──
        $sections = $this->resolveProjectSections($project->project_id);

        // ── 1. File inventory — from silver.ingest_progress (the live
        //     per-file Hatchet run history) grouped by file extension,
        //     falling back to the legacy bronze.ingest_manifest breakdown
        //     for projects that still carry PLSS-section provenance. ──
        $fileTypes = $this->loadFileTypeInventory($project->project_id, $sections);

        // ── 2. Recent ingestion runs — one row per file, sourced from
        //     silver.ingest_progress (same table IngestionRunsController
        //     uses). Falls back to the legacy bronze.ingest_runs walk for
        //     sectioned archive projects. ──────────────────────────────
        $recentRuns = $this->loadRecentRuns($project->project_id, $sections);

        // ── 3. Parser activity — from silver.reports.parser_used, which
        //     every live ingest writes, instead of the never-populated
        //     bronze.provenance join. ─────────────────────────────────
        $parserActivity = collect(DB::select(
            'SELECT COALESCE(parser_used, \'unknown\') AS parser_used,
                    COUNT(*) AS rows_written,
                    MAX(created_at) AS last_run
               FROM silver.reports
              WHERE project_id = ?
              GROUP BY COALESCE(parser_used, \'unknown\')
              ORDER BY rows_written DESC
              LIMIT 30',
            [$project->project_id],
        ))->map(fn ($p) => [
            'parser' => (string) $p->parser_used,
            'version' => '',
            'rows_written' => (int) $p->rows_written,
            'last_run' => (string) $p->last_run,
            // Every parsed report writes both silver.reports and
            // silver.document_passages rows — there's no per-parser
            // table-count tracking below report-level, so this is a
            // constant rather than a real distinct-table count.
            'tables_touched' => 2,
        ])->values();

        // ── 4. Project-scoped reports ────────────────────────────────
        $reports = DB::table('silver.reports')
            ->where('project_id', $project->project_id)
            ->select('report_id', 'title', 'company', 'filing_date', 'commodity', 'created_at', 'version')
            ->orderByDesc('created_at')
            ->limit(30)
            ->get()
            ->map(fn ($r) => [
                'id' => (string) $r->report_id,
                'title' => (string) ($r->title ?? '—'),
                'company' => (string) ($r->company ?? '—'),
                'filing_date' => (string) ($r->filing_date ?? ''),
                'commodity' => (string) ($r->commodity ?? ''),
                'version' => (int) ($r->version ?? 1),
                'created_at' => (string) ($r->created_at ?? ''),
            ])->values();

        // ── 5. Workspace passages + project-scoped quality rollup ──
        // RLS fix 2026-08-15 (third pass): silver.document_passages was
        // converted to fail-closed. $workspaceId was already resolved above
        // from the (still fail-open) silver.projects lookup.
        //
        // 2026-08-17 — dropped the bronze.provenance join (see class
        // docblock): document_passages has no provenance row on the live
        // ingest path, so this always undercounted to 0. Direct join
        // through silver.reports, same fix as CorpusController.
        $passagesInProject = (int) $this->withWorkspaceRls(
            $workspaceId,
            fn () => DB::table('silver.document_passages AS dp')
                ->join('silver.reports AS r', 'r.report_id', '=', 'dp.document_id')
                ->where('r.project_id', $project->project_id)
                ->count(),
        );

        // 2026-08-17 — silver.document_ingestion_quality is written only
        // by the §04p dual-write OCR quality stack, which is disabled in
        // production (P04P_DUAL_WRITE_ENABLED=false) — the table has no
        // writer on the live path, so this join was always empty.
        // silver.reports.parse_quality_pct IS written by every real
        // ingest; use it as the quality signal instead. No per-page
        // low-confidence breakdown exists at this granularity, so that
        // half of the rollup drops to 0 rather than being fabricated.
        $qualityRollup = DB::table('silver.reports')
            ->where('project_id', $project->project_id)
            ->whereNotNull('parse_quality_pct')
            ->selectRaw('COUNT(*) AS n_reports, AVG(parse_quality_pct) AS avg_score')
            ->first();

        // ── 6. Headline stats — all project-scoped ──────────────────
        $totalFilesProject = (int) ($fileTypes->sum('count'));
        $totalBytesProject = (int) ($fileTypes->sum('bytes'));
        $reportsCountProject = (int) DB::table('silver.reports')
            ->where('project_id', $project->project_id)
            ->count();
        $collarsCount = (int) DB::table('silver.collars')
            ->where('project_id', $project->project_id)
            ->count();
        $totalRunsTouchingProject = (int) $recentRuns->count();

        $stats = [
            'sections' => $sections,
            'total_files_in_project' => $totalFilesProject,
            'total_bytes_in_project' => $totalBytesProject,
            'reports_in_project' => $reportsCountProject,
            'passages_in_project' => $passagesInProject,
            'collars_in_project' => $collarsCount,
            'parsers_active' => $parserActivity->count(),
            'ingest_runs_in_project' => $totalRunsTouchingProject,
            'avg_quality_score' => $qualityRollup && $qualityRollup->avg_score !== null
                ? round((float) $qualityRollup->avg_score, 3) : null,
            // No per-page low-confidence breakdown exists outside the
            // disabled §04p quality stack (see qualityRollup above) —
            // left at 0 rather than fabricated (renders as "not assessed
            // yet" in the UI instead of a misleading page count).
            'low_confidence_pages' => 0,
            'total_pages_reviewed' => 0,
        ];

        return Inertia::render('Foundry/Sources', [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
            ],
            'stats' => $stats,
            'file_types' => $fileTypes,
            'recent_runs' => $recentRuns,
            'parser_activity' => $parserActivity,
            'reports' => $reports,
            'empty' => $totalFilesProject === 0 && $reportsCountProject === 0,
            'scope_note' => ! empty($sections)
                ? 'All panels are scoped to this project (PLSS section '.implode(', ', $sections).'). Workspace-wide views live under /workspace/data.'
                : ($reportsCountProject > 0
                    ? 'All panels are scoped to this project\'s uploaded reports.'
                    : 'No data has been ingested into this project yet — Connect Source to start.'),
        ]);
    }

    /**
     * File-type inventory. Prefers silver.ingest_progress (the live
     * per-file Hatchet run history) grouped by file extension; falls back
     * to the legacy bronze.ingest_manifest / PLSS-section breakdown for
     * projects that came from the bulk archive import.
     *
     * @param list<string> $sections
     *
     * @return Collection<int, array{kind: string, count: int, bytes: int}>
     */
    private function loadFileTypeInventory(string $projectId, array $sections): Collection
    {
        $rows = collect(DB::select(
            "SELECT lower(regexp_replace(filename, '^.*\\.', '')) AS ext, COUNT(*) AS n
               FROM silver.ingest_progress
              WHERE project_id = ?
              GROUP BY 1
              ORDER BY n DESC",
            [$projectId],
        ));

        if ($rows->isNotEmpty()) {
            return $rows->map(fn ($r) => [
                'kind' => (string) $r->ext,
                'count' => (int) $r->n,
                // Per-file byte size isn't tracked in ingest_progress —
                // the legacy panel below has it via bronze.ingest_manifest,
                // this path doesn't have an equivalent source.
                'bytes' => 0,
            ])->values();
        }

        if (empty($sections)) {
            return collect();
        }

        return collect(DB::table('bronze.ingest_manifest')
            ->select('file_type', DB::raw('COUNT(*) AS n'), DB::raw('SUM(file_size_bytes) AS bytes'))
            ->whereIn('guessed_project', $sections)
            ->groupBy('file_type')
            ->orderByDesc(DB::raw('COUNT(*)'))
            ->get())
            ->map(fn ($r) => [
                'kind' => (string) $r->file_type,
                'count' => (int) $r->n,
                'bytes' => (int) ($r->bytes ?? 0),
            ])->values();
    }

    /**
     * Recent ingestion runs, one row per file. Prefers silver.ingest_progress
     * (same source IngestionRunsController uses); falls back to the legacy
     * bronze.ingest_runs walk for sectioned archive-import projects.
     *
     * @param list<string> $sections
     *
     * @return Collection<int, array<string, mixed>>
     */
    private function loadRecentRuns(string $projectId, array $sections): Collection
    {
        $rows = collect(DB::select(
            "SELECT run_id::text AS id, filename, current_step, status,
                    to_char(started_at, 'YYYY-MM-DD\"T\"HH24:MI:SSOF') AS started_at,
                    to_char(completed_at, 'YYYY-MM-DD\"T\"HH24:MI:SSOF') AS completed_at,
                    error_text
               FROM silver.ingest_progress
              WHERE project_id = ?
              ORDER BY started_at DESC
              LIMIT 20",
            [$projectId],
        ));

        if ($rows->isNotEmpty()) {
            return $rows->map(fn ($r) => [
                'id' => (string) $r->id,
                'source_path' => (string) $r->filename,
                'started_at' => (string) ($r->started_at ?? ''),
                'completed_at' => (string) ($r->completed_at ?? ''),
                'status' => $r->status === 'started' ? 'running' : (string) $r->status,
                'files_seen' => 1,
                'files_indexed' => $r->status === 'completed' ? 1 : 0,
                'files_skipped' => 0,
                'bytes_seen' => 0,
                'error_text' => (string) ($r->error_text ?? ''),
            ])->values();
        }

        if (empty($sections)) {
            return collect();
        }

        $likes = array_map(fn ($s) => '%'.$s.'%', $sections);
        $runsQuery = DB::table('bronze.ingest_runs')->orderByDesc('started_at')->limit(20);
        $runsQuery->where(function ($q) use ($likes) {
            foreach ($likes as $like) {
                $q->orWhere('source_path', 'like', $like);
            }
        });

        return collect($runsQuery->get())->map(fn ($r) => [
            'id' => (string) $r->run_id,
            'source_path' => (string) ($r->source_path ?? ''),
            'started_at' => (string) ($r->started_at ?? ''),
            'completed_at' => (string) ($r->completed_at ?? ''),
            'status' => (string) ($r->status ?? 'unknown'),
            'files_seen' => (int) ($r->files_seen ?? 0),
            'files_indexed' => (int) ($r->files_indexed ?? 0),
            'files_skipped' => (int) ($r->files_skipped ?? 0),
            'bytes_seen' => (int) ($r->bytes_seen ?? 0),
            'error_text' => (string) ($r->error_text ?? ''),
        ])->values();
    }

    /**
     * Walk silver.collars + silver.reports for this project, pull the
     * source_file paths from bronze.provenance, and extract the PLSS
     * section token (`028N079W36`, `033N089W28`, …) from each path.
     *
     * Returns a deduped list. Empty when the project has no silver rows.
     *
     * @return list<string>
     */
    private function resolveProjectSections(string $projectId): array
    {
        $rows = DB::select(
            "SELECT DISTINCT
                substring(bp.source_file FROM '(?:extract|data)/([0-9]{3}N[0-9]{3}W[0-9A-Z]+)/') AS section
              FROM bronze.provenance bp
              LEFT JOIN silver.collars c ON c.collar_id = bp.target_id AND bp.target_table = 'collars'
              LEFT JOIN silver.reports r ON r.report_id = bp.target_id AND bp.target_table = 'reports'
              WHERE c.project_id = ?::uuid OR r.project_id = ?::uuid",
            [$projectId, $projectId],
        );

        $sections = [];
        foreach ($rows as $row) {
            if (! empty($row->section)) {
                $sections[] = (string) $row->section;
            }
        }

        return array_values(array_unique($sections));
    }
}
