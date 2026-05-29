<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Services\Figures\FigureResolver;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/ReportController — project-scoped reports surface.
 *
 *   GET /projects/{slug}/reports               → index of silver.reports for this project
 *   GET /projects/{slug}/reports/{report_id}   → read-only view of one report's sections_text
 *
 * silver.reports rows are *ingested* NI 43-101 filings (chunked into
 * silver.document_passages, joined to this project via project_id).
 * Drafting a new report from scratch lives on /admin/reports (separate
 * admin.report_builds table). The "+ New draft" CTA links there for
 * admin users; non-admins don't see it.
 */
class ReportController extends Controller
{
    public function index(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()
            ->where('silver.projects.project_id', $project->project_id)
            ->firstOrFail();

        $reports = DB::table('silver.reports')
            ->where('project_id', $project->project_id)
            ->orderByDesc('updated_at')
            ->limit(60)
            ->get();

        return Inertia::render('Foundry/Report', [
            'project' => [
                'project_id'   => $project->project_id,
                'project_name' => $project->project_name,
                'slug'         => $project->slug,
            ],
            'reports' => $reports->map(function ($r) {
                $sectionsRaw = $r->sections_text ?? null;
                $sections = is_string($sectionsRaw)
                    ? json_decode($sectionsRaw, true)
                    : $sectionsRaw;
                $sectionsCount = is_array($sections)
                    ? count($sections)
                    : (is_object($sections) ? count(get_object_vars($sections)) : 0);
                return [
                    'report_id'         => (string) ($r->report_id ?? ''),
                    'title'             => (string) ($r->title ?? 'Untitled report'),
                    'company'           => (string) ($r->company ?? ''),
                    'filing_date'       => (string) ($r->filing_date ?? ''),
                    'commodity'         => (string) ($r->commodity ?? ''),
                    'parse_quality_pct' => isset($r->parse_quality_pct) ? (float) $r->parse_quality_pct : null,
                    'version'           => (int) ($r->version ?? 1),
                    'is_scanned'        => (bool) ($r->is_scanned ?? false),
                    'sections_count'    => $sectionsCount,
                    'has_content'       => $sectionsCount > 0,
                ];
            })->values(),
            'is_admin' => (bool) ($request->user()->is_admin ?? false),
            'empty'    => $reports->isEmpty(),
        ]);
    }

    public function view(Request $request, string $slug, string $report_id): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()
            ->where('silver.projects.project_id', $project->project_id)
            ->firstOrFail();

        if (! preg_match('/^[0-9a-f-]{36}$/i', $report_id)) {
            abort(404);
        }

        $row = DB::table('silver.reports')
            ->where('report_id', $report_id)
            ->where('project_id', $project->project_id)
            ->first();

        if (! $row) {
            abort(404, 'Report not found in this project.');
        }

        $sectionsRaw = $row->sections_text ?? null;
        $sectionsDecoded = is_string($sectionsRaw)
            ? json_decode($sectionsRaw, true)
            : $sectionsRaw;

        // Normalise to a list of {heading, body, kind} dicts so the
        // React page can render either an object-shaped or array-
        // shaped sections_text consistently.
        $sections = [];
        if (is_array($sectionsDecoded)) {
            // Numeric array of section dicts
            if (array_is_list($sectionsDecoded)) {
                foreach ($sectionsDecoded as $i => $s) {
                    $sections[] = $this->normaliseSection($s, $i);
                }
            } else {
                // Object keyed by heading
                $i = 0;
                foreach ($sectionsDecoded as $heading => $body) {
                    $sections[] = $this->normaliseSection(['heading' => $heading, 'body' => $body], $i++);
                }
            }
        }

        // Pull a few real passages from silver.document_passages tied
        // to this report (via bronze.provenance reverse-lookup) so
        // the user can see the actual indexed chunk text.
        $passages = collect();
        try {
            $passages = DB::table('silver.document_passages AS dp')
                ->join('bronze.provenance AS bp', function ($j) {
                    $j->on(DB::raw('bp.target_id::text'), '=', DB::raw('dp.passage_id::text'))
                      ->where('bp.target_table', '=', 'document_passages');
                })
                ->join('bronze.provenance AS bp2', function ($j) {
                    $j->on(DB::raw('bp2.source_file'), '=', DB::raw('bp.source_file'))
                      ->where('bp2.target_table', '=', 'reports');
                })
                ->where('bp2.target_id', $report_id)
                ->select('dp.passage_id', 'dp.text', 'dp.page_first', 'dp.page_last', 'dp.ordinal', 'dp.chunk_kind')
                ->orderBy('dp.ordinal')
                ->limit(30)
                ->get();
        } catch (\Throwable $e) {
            $passages = collect();
        }

        // Inline figures alongside the sections so the React page can render
        // them without a separate XHR. Manifest is best-effort — empty array
        // when ingest hasn't extracted figures yet.
        try {
            $figures = app(FigureResolver::class)->manifestFor($report_id);
        } catch (\Throwable $e) {
            $figures = [];
        }

        // Plan §6a — per-report data-quality flag summary for the badge UI.
        // Joins silver.document_passages to silver.data_quality_flags via
        // record_id (passage_id text-cast) so all flags whose record_type
        // is document_chunk / table_extraction and whose passage belongs
        // to this report bubble up to the report header.
        //
        // Mirrors the DrillholeDetail pattern. Returns null on empty so
        // the badge component hides itself for well-behaved reports.
        $dqFlags = $this->dataQualityFlagSummary($report_id);

        return Inertia::render('Foundry/ReportView', [
            'project' => [
                'project_id'   => $project->project_id,
                'project_name' => $project->project_name,
                'slug'         => $project->slug,
            ],
            'figures' => $figures,
            'report' => [
                'report_id'   => (string) $row->report_id,
                'title'       => (string) ($row->title ?? 'Untitled report'),
                'company'     => (string) ($row->company ?? ''),
                'filing_date' => (string) ($row->filing_date ?? ''),
                'commodity'   => (string) ($row->commodity ?? ''),
                'version'     => (int) ($row->version ?? 1),
                'region'      => (string) ($row->region ?? ''),
                'project_name'=> (string) ($row->project_name ?? ''),
                'created_at'  => (string) ($row->created_at ?? ''),
                'updated_at'  => (string) ($row->updated_at ?? ''),
            ],
            'sections' => $sections,
            'passages' => $passages->map(fn ($p) => [
                'id'         => (string) $p->passage_id,
                'text'       => (string) $p->text,
                'ordinal'    => (int) ($p->ordinal ?? 0),
                'page_first' => $p->page_first !== null ? (int) $p->page_first : null,
                'page_last'  => $p->page_last !== null ? (int) $p->page_last : null,
                'chunk_kind' => (string) ($p->chunk_kind ?? ''),
            ])->values(),
            'data_quality_flags' => $dqFlags,
            'is_admin' => (bool) ($request->user()->is_admin ?? false),
            'empty'    => empty($sections) && $passages->isEmpty(),
        ]);
    }

    /**
     * Plan §6a — per-report data-quality flag summary for the badge UI.
     *
     * Returns the array shape the DataQualityFlagsBadge React component
     * consumes::
     *
     *     [
     *       'counts'     => ['ERROR' => 0, 'WARNING' => 1, 'INFO' => 2],
     *       'open_total' => 3,
     *       'flags'      => [
     *         ['flag_type' => 'document_chunk.low_ocr_confidence',
     *          'severity'  => 'WARNING',
     *          'description' => '...'], ...
     *       ],
     *     ]
     *
     * Joins silver.document_passages to silver.data_quality_flags so any
     * flag whose record_id maps to a passage of this report surfaces on
     * the report header. record_type filtered to the two document-scoped
     * values (document_chunk + table_extraction) per ADR-0010 §6a.
     *
     * @return array{counts: array<string, int>, open_total: int, flags: array<int, array<string, mixed>>}
     */
    private function dataQualityFlagSummary(string $reportId): array
    {
        // Counts by severity for the badge dots — joined via passage_id.
        $countRows = DB::table('silver.data_quality_flags as f')
            ->join('silver.document_passages as p', DB::raw('p.passage_id::text'), '=', DB::raw('f.record_id'))
            ->where('p.document_id', $reportId)
            ->whereIn('f.record_type', ['document_chunk', 'table_extraction'])
            ->whereNull('f.resolved_at')
            ->select('f.severity', DB::raw('count(*)::int as n'))
            ->groupBy('f.severity')
            ->get();

        $counts = ['ERROR' => 0, 'WARNING' => 0, 'INFO' => 0];
        foreach ($countRows as $row) {
            if (isset($counts[$row->severity])) {
                $counts[$row->severity] = (int) $row->n;
            }
        }

        // Cap the rendered flag list — a verbose report can produce many
        // chunk-level flags. Order ERROR first, then WARNING, then INFO,
        // latest-first within each tier.
        $flags = DB::table('silver.data_quality_flags as f')
            ->join('silver.document_passages as p', DB::raw('p.passage_id::text'), '=', DB::raw('f.record_id'))
            ->where('p.document_id', $reportId)
            ->whereIn('f.record_type', ['document_chunk', 'table_extraction'])
            ->whereNull('f.resolved_at')
            ->orderByRaw("CASE f.severity WHEN 'ERROR' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END")
            ->orderByDesc('f.flagged_at')
            ->limit(20)
            ->select(
                'f.flag_id',
                'f.flag_type',
                'f.severity',
                'f.description',
                'f.rule_id',
                'f.rule_version',
                'f.flagged_at',
            )
            ->get()
            ->map(fn ($f) => [
                'flag_id'      => $f->flag_id,
                'flag_type'    => $f->flag_type,
                'severity'     => $f->severity,
                'description'  => $f->description,
                'rule_id'      => $f->rule_id,
                'rule_version' => $f->rule_version,
                'flagged_at'   => $f->flagged_at,
            ])
            ->all();

        return [
            'counts'     => $counts,
            'open_total' => array_sum($counts),
            'flags'      => $flags,
        ];
    }

    /**
     * GET /projects/{slug}/reports/{report_id}/figures
     *
     * Returns the figure manifest with fresh presigned PNG URLs. Used by
     * the chat citation renderer to lazy-load figure thumbnails without
     * re-renders of the full report page.
     *
     * RLS via the user→project membership check; no figure URLs cross the
     * project boundary.
     */
    public function figures(Request $request, string $slug, string $report_id): JsonResponse
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()
            ->where('silver.projects.project_id', $project->project_id)
            ->firstOrFail();

        if (! preg_match('/^[0-9a-f-]{36}$/i', $report_id)) {
            abort(404);
        }

        $exists = DB::table('silver.reports')
            ->where('report_id', $report_id)
            ->where('project_id', $project->project_id)
            ->exists();
        if (! $exists) {
            abort(404, 'Report not found in this project.');
        }

        $figures = app(FigureResolver::class)->manifestFor($report_id);

        return response()->json([
            'report_id' => $report_id,
            'figures'   => $figures,
        ]);
    }

    /**
     * @param  mixed  $raw
     * @return array{heading:string,body:string,kind:string,index:int}
     */
    private function normaliseSection($raw, int $index): array
    {
        if (is_string($raw)) {
            return ['heading' => '', 'body' => $raw, 'kind' => 'para', 'index' => $index];
        }
        if (is_array($raw)) {
            $heading = (string) ($raw['heading'] ?? $raw['title'] ?? '');
            $body = $raw['body'] ?? $raw['text'] ?? $raw['content'] ?? '';
            if (is_array($body)) {
                $body = json_encode($body, JSON_UNESCAPED_SLASHES | JSON_PRETTY_PRINT);
            }
            $kind = (string) ($raw['kind'] ?? $raw['type'] ?? 'para');
            return ['heading' => $heading, 'body' => (string) $body, 'kind' => $kind, 'index' => $index];
        }
        return ['heading' => '', 'body' => (string) $raw, 'kind' => 'para', 'index' => $index];
    }
}
