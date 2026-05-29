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
 * Foundry/CorpusController — the "Reader" surface inside a project.
 *
 * The Foundry shell labels this nav item "Reader" (it routes here for
 * consistency with the original /corpus path). The page is a project-
 * scoped reading interface: list of documents in this project, recent
 * indexed passages, and entity-link rollups — all clickable into the
 * full ReportView surface.
 *
 * Project-scoping for document_passages joins through silver.reports
 * via passage.document_id = report.report_id. The earlier version used
 * bronze.provenance but the PDF ingest pipeline never writes provenance
 * for reports / document_passages (only tabular ingesters do), so the
 * count was always 0 for any PDF-only project.
 */
class CorpusController extends Controller
{
    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()
            ->where('silver.projects.project_id', $project->project_id)
            ->firstOrFail();

        // ── Reports for this project ─────────────────────────────────
        $reports = DB::table('silver.reports')
            ->where('project_id', $project->project_id)
            ->orderByDesc('updated_at')
            ->limit(50)
            ->get();

        $reportsCount = (int) DB::table('silver.reports')
            ->where('project_id', $project->project_id)
            ->count();

        // ── Passages for this project (direct report-id join) ────────
        $passagesCount = (int) DB::table('silver.document_passages AS dp')
            ->join('silver.reports AS r', 'r.report_id', '=', 'dp.document_id')
            ->where('r.project_id', $project->project_id)
            ->count();

        $recentPassages = DB::table('silver.document_passages AS dp')
            ->join('silver.reports AS r', 'r.report_id', '=', 'dp.document_id')
            ->where('r.project_id', $project->project_id)
            ->select(
                'dp.passage_id',
                'dp.text',
                'dp.ordinal',
                'dp.page_first',
                'dp.page_last',
                'dp.chunk_kind',
                'r.report_id',
                'r.title AS report_title',
            )
            ->orderBy('dp.ordinal')
            ->limit(30)
            ->get();

        // ── Entity-link rollup ───────────────────────────────────────
        // Lives in public_geo (cross-corpus linker storage, see migration
        // 2026_04_14_140000_create_document_entity_links). The table has
        // no project_id of its own; project-scope by joining document_id
        // back to silver.reports.report_id. Active rows only —
        // superseded_at IS NULL filters out historical linker decisions.
        $entityLinks = (int) DB::table('public_geo.document_entity_links AS del')
            ->join('silver.reports AS r', 'r.report_id', '=', 'del.document_id')
            ->where('r.project_id', $project->project_id)
            ->whereNull('del.superseded_at')
            ->count();

        $entitySummary = DB::table('public_geo.document_entity_links AS del')
            ->join('silver.reports AS r', 'r.report_id', '=', 'del.document_id')
            ->where('r.project_id', $project->project_id)
            ->whereNull('del.superseded_at')
            ->select('del.canonical_type', DB::raw('COUNT(*) AS n'))
            ->groupBy('del.canonical_type')
            ->orderByDesc(DB::raw('COUNT(*)'))
            ->limit(20)
            ->get()
            ->map(fn ($r) => [
                'kind' => (string) ($r->canonical_type ?? 'unknown'),
                'count' => (int) $r->n,
            ])->values();

        // Map reports for the React layer ─────────────────────────────
        $reportList = $reports->map(function ($r) {
            $sectionsRaw = $r->sections_text ?? null;
            $sections = is_string($sectionsRaw)
                ? json_decode($sectionsRaw, true)
                : $sectionsRaw;
            $count = is_array($sections)
                ? count($sections)
                : (is_object($sections) ? count(get_object_vars($sections)) : 0);

            return [
                'id' => (string) $r->report_id,
                'title' => (string) ($r->title ?? 'Untitled report'),
                'company' => (string) ($r->company ?? ''),
                'filing_date' => (string) ($r->filing_date ?? ''),
                'version' => (int) ($r->version ?? 1),
                'is_scanned' => (bool) ($r->is_scanned ?? false),
                'sections_count' => $count,
                'has_content' => $count > 0,
                'updated_at' => (string) ($r->updated_at ?? ''),
            ];
        })->values();

        return Inertia::render('Foundry/Corpus', [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
            ],
            'stats' => [
                'reports' => $reportsCount,
                'reports_with_content' => $reportList->where('has_content', true)->count(),
                'passages' => $passagesCount,
                'entity_links' => $entityLinks,
            ],
            'reports' => $reportList,
            'passages' => $recentPassages->map(fn ($p) => [
                'id' => (string) $p->passage_id,
                'text' => (string) $p->text,
                'ordinal' => (int) ($p->ordinal ?? 0),
                'page_first' => $p->page_first !== null ? (int) $p->page_first : null,
                'page_last' => $p->page_last !== null ? (int) $p->page_last : null,
                'chunk_kind' => (string) ($p->chunk_kind ?? ''),
                'report_id' => (string) ($p->report_id ?? ''),
                'report_title' => (string) ($p->report_title ?? ''),
            ])->values(),
            'entity_summary' => $entitySummary,
            'empty' => $reportsCount === 0,
        ]);
    }
}
