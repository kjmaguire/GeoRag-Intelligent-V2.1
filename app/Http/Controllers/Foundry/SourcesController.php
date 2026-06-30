<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/SourcesController — the "Data" surface inside a project.
 *
 * **Project-scoped.** Every panel filters to the bronze ingest that
 * produced this project's silver rows.
 *
 * Bronze does not carry a `project_id` column directly — files are
 * grouped by `cluster_key` (one PLSS section per inner zip in the
 * Wyoming archive, e.g. `innerzip::uranium-logs_TRS/028N079W36.zip`).
 * We derive the project's section(s) from `bronze.provenance.source_file`
 * (every silver.collar's parser wrote a provenance row whose source
 * path starts with `/extract/<SECTION>/...` or `//data/<SECTION>/...`),
 * then filter all bronze tables by those sections.
 *
 * Project with no silver rows yet → empty bronze view (no data ingested
 * into this project), which is the correct semantic.
 */
class SourcesController extends Controller
{
    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()
            ->where('silver.projects.project_id', $project->project_id)
            ->firstOrFail();

        $workspaceId = $project->workspace_id;

        // ── 0. Resolve which PLSS sections (cluster_keys) belong to this
        //     project, by walking provenance for its silver rows. ──────
        $sections = $this->resolveProjectSections($project->project_id);
        $clusterKeys = array_values(array_unique(array_map(
            fn ($s) => "innerzip::uranium-logs_TRS/{$s}.zip",
            $sections,
        )));

        // ── 1. File-type inventory scoped to this project's sections ─
        $fileTypesQuery = DB::table('bronze.ingest_manifest')
            ->select('file_type', DB::raw('COUNT(*) AS n'), DB::raw('SUM(file_size_bytes) AS bytes'))
            ->groupBy('file_type')
            ->orderByDesc(DB::raw('COUNT(*)'));
        if (! empty($sections)) {
            $fileTypesQuery->whereIn('guessed_project', $sections);
        } else {
            $fileTypesQuery->whereRaw('1=0'); // no project sections → no rows
        }
        $fileTypes = $fileTypesQuery->get()->map(fn ($r) => [
            'kind' => (string) $r->file_type,
            'count' => (int) $r->n,
            'bytes' => (int) ($r->bytes ?? 0),
        ])->values();

        // ── 2. Recent ingestion runs that touched this project's sections ──
        $recentRuns = collect();
        if (! empty($sections)) {
            $likes = array_map(fn ($s) => '%'.$s.'%', $sections);
            $runsQuery = DB::table('bronze.ingest_runs')->orderByDesc('started_at')->limit(20);
            $runsQuery->where(function ($q) use ($likes) {
                foreach ($likes as $like) {
                    $q->orWhere('source_path', 'like', $like);
                }
            });
            $recentRuns = $runsQuery->get()->map(fn ($r) => [
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

        // ── 3. Parser activity for this project ─────────────────────
        $parserActivity = DB::select(
            'SELECT bp.parser_name,
                    bp.parser_version,
                    COUNT(*) AS rows_written,
                    MAX(bp.ingested_at) AS last_run,
                    COUNT(DISTINCT bp.target_table) AS tables_touched
               FROM bronze.provenance bp
               LEFT JOIN silver.collars c ON c.collar_id = bp.target_id AND bp.target_table = \'collars\'
               LEFT JOIN silver.reports r ON r.report_id = bp.target_id AND bp.target_table = \'reports\'
              WHERE (c.project_id = ?::uuid OR r.project_id = ?::uuid)
              GROUP BY bp.parser_name, bp.parser_version
              ORDER BY rows_written DESC
              LIMIT 30',
            [$project->project_id, $project->project_id],
        );
        $parserActivity = collect($parserActivity)->map(fn ($p) => [
            'parser' => (string) $p->parser_name,
            'version' => (string) $p->parser_version,
            'rows_written' => (int) $p->rows_written,
            'last_run' => (string) $p->last_run,
            'tables_touched' => (int) $p->tables_touched,
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
        $passagesInProject = (int) DB::table('silver.document_passages AS dp')
            ->join('bronze.provenance AS bp', function ($j) {
                $j->on(DB::raw('bp.target_id::text'), '=', DB::raw('dp.passage_id::text'))
                    ->where('bp.target_table', '=', 'document_passages');
            })
            ->join('silver.reports AS r', function ($j) {
                $j->on('r.report_id', '=', 'bp.target_id')
                    ->where('bp.target_table', '=', 'reports');
            })
            ->where('r.project_id', $project->project_id)
            ->count();

        $qualityRollup = DB::table('silver.document_ingestion_quality AS dq')
            ->join('silver.reports AS r', 'r.report_id', '=', 'dq.report_id')
            ->where('r.project_id', $project->project_id)
            ->selectRaw('COUNT(*) AS n_reports, AVG(dq.overall_quality_score) AS avg_score, SUM(dq.low_confidence_pages) AS low_conf_pages, SUM(dq.total_pages) AS total_pages')
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
            'low_confidence_pages' => $qualityRollup ? (int) ($qualityRollup->low_conf_pages ?? 0) : 0,
            'total_pages_reviewed' => $qualityRollup ? (int) ($qualityRollup->total_pages ?? 0) : 0,
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
            'scope_note' => empty($sections)
                ? 'No bronze data has been ingested into this project yet — Connect Source to start.'
                : 'All panels are scoped to this project (PLSS section '.implode(', ', $sections).'). Workspace-wide views live under /workspace/data.',
        ]);
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
