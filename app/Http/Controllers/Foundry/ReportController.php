<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Services\Figures\FigureResolver;
use App\Services\StorageService;
use App\Support\SetsWorkspaceRlsContext;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Collection;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;
use Throwable;

/**
 * Foundry/ReportController — the project-scoped documents surface.
 *
 *   GET /projects/{slug}/reports               → master-detail, nothing selected
 *   GET /projects/{slug}/reports/{report_id}   → same page, that report selected
 *
 * Merged 2026-08-18. This was three pages that were all views of the same
 * two tables:
 *
 *   - /reports          (Foundry/Report)       list of silver.reports
 *   - /reports/{id}     (Foundry/ReportView)   one report's sections + passages
 *   - /imports/quality  (Foundry/IngestQuality) silver.reports + document_passages
 *                                               counts, per file, plus a
 *                                               review_queue rollup
 *   - /corpus           (Foundry/Corpus)      the nav's "Reader": the same
 *                                               document list again, plus a
 *                                               cross-document passage sample
 *                                               and an entity-link rollup
 *
 * The quality page in particular was the same per-document list as /reports
 * with two extra numbers on each row, so a user checking "did my upload
 * work" had to hop between three surfaces to answer one question. They are
 * now one page: a document list carrying its own ingest status, a
 * project-level quality strip above it, and the reader in the detail pane
 * with Quality as one of its tabs. /imports/quality redirects here.
 *
 * silver.reports rows are *ingested* NI 43-101 filings (chunked into
 * silver.document_passages, joined to this project via project_id).
 * Drafting a new report from scratch lives on /admin/reports (separate
 * admin.report_builds table).
 *
 * Props are passed as closures deliberately: selecting a report in the UI
 * is an Inertia partial reload asking only for the detail props, and a
 * closure prop that isn't requested is never evaluated. Without that, every
 * click would re-run the list + rollup queries it already has.
 */
class ReportController extends Controller
{
    use SetsWorkspaceRlsContext;

    /** How many passages the reader's "Passages" tab previews. */
    private const PASSAGE_PREVIEW_LIMIT = 30;

    /** How many documents the master list shows. */
    private const REPORT_LIST_LIMIT = 60;

    /**
     * Lifetime of a presigned original-document URL. An hour matches
     * FigureResolver's default and is long enough to read a filing without
     * the tab going stale mid-scroll.
     */
    private const SOURCE_URL_TTL_SECONDS = 3600;

    /**
     * Per-request memo for passagesFor(). Request-scoped because the
     * controller instance is, which is what keeps it safe under Octane.
     *
     * @var array<string, array{rows: array<int, array<string, mixed>>, total: int}>
     */
    private array $passageCache = [];

    public function index(Request $request, string $slug): Response
    {
        $project = $this->resolveProject($request, $slug);

        return Inertia::render('Foundry/Reports', array_merge(
            $this->listPayload($project),
            $this->emptyDetailPayload(),
            // Only when nothing is selected — this is what fills the right
            // pane in place of a document, and it is the whole of what the
            // old /corpus "Reader" page carried that the list does not.
            ['overview' => fn () => $this->projectOverview($project)],
        ));
    }

    public function view(Request $request, string $slug, string $report_id): Response
    {
        $project = $this->resolveProject($request, $slug);

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

        return Inertia::render('Foundry/Reports', array_merge(
            $this->listPayload($project),
            $this->detailPayload($project, $report_id, $row),
            // Deep-linking straight to a document shows the detail pane, so
            // the overview would be computed and thrown away.
            ['overview' => null],
        ));
    }

    /**
     * Resolve the project and assert the caller is a member of it.
     */
    private function resolveProject(Request $request, string $slug): Project
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()
            ->where('silver.projects.project_id', $project->project_id)
            ->firstOrFail();

        return $project;
    }

    /**
     * Master list + project-level quality rollup — the props that stay put
     * while the user clicks between documents.
     *
     * @return array<string, mixed>
     */
    private function listPayload(Project $project): array
    {
        // Computed once, shared by the two closures below, but only if one
        // of them is actually evaluated (i.e. not on a detail-only partial
        // reload). Memoised because Inertia resolves each closure separately.
        $rows = null;
        $resolve = function () use ($project, &$rows) {
            if ($rows === null) {
                $rows = $this->reportRows($project);
            }

            return $rows;
        };

        return [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
            ],
            'reports' => fn () => $resolve()->all(),
            'quality' => fn () => $this->qualityRollup($project, $resolve()),
            'empty' => fn () => $resolve()->isEmpty(),
        ];
    }

    /**
     * One row per ingested document, carrying its own ingest status.
     *
     * The passages/embedded counts and the status derivation come from the
     * former IngestQualityController. Two things it got right that must not
     * be lost in the merge:
     *
     *   1. silver.document_passages is fail-closed RLS, so the join runs
     *      inside withWorkspaceRls() — without it the subquery matches zero
     *      rows and every document reads 0/0 (confirmed live 2026-08-17 on a
     *      project with 20,613 real passages).
     *   2. Status is derived from passages/embedded, NOT parse_quality_pct.
     *      parse_quality_pct measures structural coverage against the
     *      17-section NI 43-101 baseline; a corporate presentation or a
     *      table extract scores low on it while having parsed and embedded
     *      perfectly. Gating status on it reported healthy ingests as
     *      failures.
     *
     * @return Collection<int, array<string, mixed>>
     */
    private function reportRows(Project $project): Collection
    {
        $rows = $this->withWorkspaceRls(
            (string) $project->workspace_id,
            fn () => DB::table('silver.reports AS r')
                ->leftJoinSub(
                    DB::table('silver.document_passages')
                        ->selectRaw('document_id, COUNT(*) AS passages, COUNT(*) FILTER (WHERE embedding_id IS NOT NULL) AS embedded')
                        ->groupBy('document_id'),
                    'p',
                    'p.document_id',
                    '=',
                    'r.report_id',
                )
                ->where('r.project_id', $project->project_id)
                ->orderByDesc('r.updated_at')
                ->limit(self::REPORT_LIST_LIMIT)
                ->select(
                    'r.report_id',
                    'r.title',
                    'r.company',
                    'r.filing_date',
                    'r.commodity',
                    'r.version',
                    'r.is_scanned',
                    'r.parse_quality_pct',
                    'r.sections_text',
                    DB::raw('COALESCE(p.passages, 0) AS passages'),
                    DB::raw('COALESCE(p.embedded, 0) AS embedded'),
                )
                ->get(),
        );

        return $rows->map(function ($r) {
            $sectionsRaw = $r->sections_text ?? null;
            $sections = is_string($sectionsRaw) ? json_decode($sectionsRaw, true) : $sectionsRaw;
            $sectionsCount = is_array($sections)
                ? count($sections)
                : (is_object($sections) ? count(get_object_vars($sections)) : 0);

            $passages = (int) $r->passages;
            $embedded = (int) $r->embedded;

            return [
                'report_id' => (string) ($r->report_id ?? ''),
                'title' => (string) ($r->title ?? 'Untitled report'),
                'company' => (string) ($r->company ?? ''),
                'filing_date' => (string) ($r->filing_date ?? ''),
                'commodity' => (string) ($r->commodity ?? ''),
                'version' => (int) ($r->version ?? 1),
                'is_scanned' => (bool) ($r->is_scanned ?? false),
                'parse_quality_pct' => isset($r->parse_quality_pct) ? (float) $r->parse_quality_pct : null,
                'sections_count' => $sectionsCount,
                'has_content' => $sectionsCount > 0,
                'passages' => $passages,
                'embedded' => $embedded,
                // unassessed = nothing extracted yet; error = extracted but
                // nothing embedded (so chat cannot retrieve it at all);
                // warn = partially embedded, the sweep is mid-flight or stuck.
                'status' => match (true) {
                    $passages === 0 => 'unassessed',
                    $embedded === 0 => 'error',
                    $embedded < $passages => 'warn',
                    default => 'ok',
                },
            ];
        })->values();
    }

    /**
     * Project-level quality rollup — the strip above the document list.
     *
     * review_queue rows carry project_id + bronze_uri but no report_id, so
     * flagged/rejected stay project-level totals rather than a fabricated
     * per-document breakdown. That is why the document rows below expose
     * passages/embedded (which ARE per-document) and not flagged/rejected.
     *
     * @param Collection<int, array<string, mixed>> $rows
     *
     * @return array<string, mixed>
     */
    private function qualityRollup(Project $project, $rows): array
    {
        $review = DB::table('silver.review_queue')
            ->where('project_id', $project->project_id)
            ->selectRaw("
                COUNT(*) FILTER (WHERE lifecycle NOT IN ('committed', 'archived')) AS flagged,
                COUNT(*) FILTER (WHERE decision_kind = 'reject') AS rejected,
                COUNT(*) FILTER (WHERE routing_decision = 'review_required' AND lifecycle = 'pending') AS awaiting_ocr
            ")
            ->first();

        $passagesTotal = (int) $rows->sum('passages');
        $flagged = (int) ($review->flagged ?? 0);
        $rejected = (int) ($review->rejected ?? 0);
        $awaiting = (int) ($review->awaiting_ocr ?? 0);

        // "Accepted" = content units that never entered the review queue.
        // Clamped so a project with more open review items than passages
        // doesn't go negative.
        $accepted = max(0, $passagesTotal - $flagged - $rejected);
        $rowsTotal = $accepted + $flagged + $rejected;

        return [
            'totals' => [
                'accepted' => $accepted,
                'flagged' => $flagged,
                'rejected' => $rejected,
                'awaiting_ocr' => $awaiting,
            ],
            'passages_total' => $passagesTotal,
            'embedded_total' => (int) $rows->sum('embedded'),
            'documents' => $rows->count(),
            'documents_not_retrievable' => $rows->where('status', 'error')->count(),
            // Promotion gate: passes when acceptance >= 95% AND nothing rejected.
            'pass_gate' => $rowsTotal === 0
                ? false
                : (($accepted / max(1, $rowsTotal)) >= 0.95 && $rejected === 0),
        ];
    }

    /**
     * Project-level corpus overview — shown in the right pane when no single
     * document is selected.
     *
     * Absorbed 2026-08-18 from the former /corpus "Reader" page, whose own
     * docblock described it as "a project-scoped reading interface: list of
     * documents in this project, recent indexed passages, and entity-link
     * rollups — all clickable into the full ReportView surface". The document
     * list half is now the master list; these are the parts that had no home
     * in it: a cross-document passage sample (what did we actually index?)
     * and the entity-link rollup.
     *
     * document_entity_links lives in public_geo and has no project_id of its
     * own, so it is scoped by joining document_id back to
     * silver.reports.report_id. superseded_at IS NULL keeps historical linker
     * decisions out.
     *
     * @return array<string, mixed>
     */
    private function projectOverview(Project $project): array
    {
        $workspaceId = (string) $project->workspace_id;

        $recent = $this->withWorkspaceRls(
            $workspaceId,
            fn () => DB::table('silver.document_passages AS dp')
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
                ->orderByDesc('r.updated_at')
                ->orderBy('dp.ordinal')
                ->limit(self::PASSAGE_PREVIEW_LIMIT)
                ->get(),
        );

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
            ])->values()->all();

        return [
            'entity_links' => $entityLinks,
            'entity_summary' => $entitySummary,
            'recent_passages' => $recent->map(fn ($p) => [
                'id' => (string) $p->passage_id,
                'text' => (string) $p->text,
                'ordinal' => (int) ($p->ordinal ?? 0),
                'page_first' => $p->page_first !== null ? (int) $p->page_first : null,
                'page_last' => $p->page_last !== null ? (int) $p->page_last : null,
                'chunk_kind' => (string) ($p->chunk_kind ?? ''),
                'report_id' => (string) ($p->report_id ?? ''),
                'report_title' => (string) ($p->report_title ?? ''),
            ])->values()->all(),
        ];
    }

    /**
     * Detail-pane props for the selected document.
     *
     * @return array<string, mixed>
     */
    private function detailPayload(Project $project, string $report_id, object $row): array
    {
        return [
            'selected_id' => $report_id,
            'report' => [
                'report_id' => (string) $row->report_id,
                'title' => (string) ($row->title ?? 'Untitled report'),
                'company' => (string) ($row->company ?? ''),
                'filing_date' => (string) ($row->filing_date ?? ''),
                'commodity' => (string) ($row->commodity ?? ''),
                'version' => (int) ($row->version ?? 1),
                'region' => (string) ($row->region ?? ''),
                'project_name' => (string) ($row->project_name ?? ''),
                'parse_quality_pct' => isset($row->parse_quality_pct) ? (float) $row->parse_quality_pct : null,
                'is_scanned' => (bool) ($row->is_scanned ?? false),
                'page_count' => isset($row->page_count) ? (int) $row->page_count : null,
                'parser_used' => (string) ($row->parser_used ?? ''),
                'created_at' => (string) ($row->created_at ?? ''),
                'updated_at' => (string) ($row->updated_at ?? ''),
                // Drives the reader's ORIGINAL tab. A boolean rather than
                // the key itself: the bronze object key is internal
                // storage layout and the client has no use for it beyond
                // "is there something to fetch", which the dedicated
                // endpoint answers properly with a short-lived URL.
                'has_source' => ($row->source_object_key ?? null) !== null,
            ],
            'sections' => fn () => $this->sectionsFor($row),
            'passages' => fn () => $this->passagesFor($project, $report_id)['rows'],
            'passages_total' => fn () => $this->passagesFor($project, $report_id)['total'],
            'figures' => fn () => $this->figuresFor($report_id),
            'data_quality_flags' => fn () => $this->dataQualityFlagSummary(
                $report_id,
                (string) $project->workspace_id,
            ),
        ];
    }

    /**
     * @return array<string, mixed>
     */
    private function emptyDetailPayload(): array
    {
        return [
            'selected_id' => null,
            'report' => null,
            'sections' => [],
            'passages' => [],
            'passages_total' => 0,
            'figures' => [],
            'data_quality_flags' => null,
        ];
    }

    /**
     * Normalise sections_text to a list of {heading, body, kind} dicts so the
     * React page can render either an object-shaped or array-shaped value
     * consistently.
     *
     * @return array<int, array{heading:string,body:string,kind:string,index:int}>
     */
    private function sectionsFor(object $row): array
    {
        $raw = $row->sections_text ?? null;
        $decoded = is_string($raw) ? json_decode($raw, true) : $raw;

        if (! is_array($decoded)) {
            return [];
        }

        $sections = [];
        if (array_is_list($decoded)) {
            foreach ($decoded as $i => $s) {
                $sections[] = $this->normaliseSection($s, $i);
            }

            return $sections;
        }

        $i = 0;
        foreach ($decoded as $heading => $body) {
            $sections[] = $this->normaliseSection(['heading' => $heading, 'body' => $body], $i++);
        }

        return $sections;
    }

    /**
     * Passage preview + true total for the selected document.
     *
     * Keyed on dp.document_id, which IS the report_id: ingest_pdf.py's
     * INSERT_PASSAGE_SQL / INSERT_IMAGE_PASSAGE_SQL both bind $1 = report_id
     * into document_id. That is the canonical passage→report link and the one
     * dataQualityFlagSummary() below also uses.
     *
     * Bug fixed 2026-08-18: this used to reverse-look-up through two
     * bronze.provenance rows (target_table 'document_passages' joined to
     * target_table 'reports' on a shared source_file). The PDF ingest path
     * writes NEITHER of those rows — it emits audit.audit_ledger entries, not
     * bronze.provenance (see SourcesControllerTest, which documents the same
     * gap) — so the join matched zero rows for every PDF-ingested report and
     * the passages tab was empty even when the report had hundreds of
     * embedded passages.
     *
     * RLS fix 2026-08-15 (third pass): silver.document_passages is
     * fail-closed. $project is resolved via the (still fail-open)
     * silver.projects lookup, so its workspace_id is safe to bind here.
     *
     * @return array{rows: array<int, array<string, mixed>>, total: int}
     */
    private function passagesFor(Project $project, string $report_id): array
    {
        // Memoised: 'passages' and 'passages_total' are separate Inertia
        // props, so without this the query pair would run twice per request.
        // Instance property, NOT a static local — under Octane a static
        // would survive into the next request and serve another tenant's
        // passages.
        $key = $project->workspace_id.':'.$report_id;
        if (isset($this->passageCache[$key])) {
            return $this->passageCache[$key];
        }

        try {
            [$rows, $total] = $this->withWorkspaceRls(
                (string) $project->workspace_id,
                function () use ($report_id) {
                    $rows = DB::table('silver.document_passages AS dp')
                        ->where('dp.document_id', $report_id)
                        ->select('dp.passage_id', 'dp.text', 'dp.page_first', 'dp.page_last', 'dp.ordinal', 'dp.chunk_kind')
                        // Narrative chunks first, then page-image rows: an
                        // image passage's ordinal is its page number, so a
                        // plain ordinal sort would interleave the two kinds.
                        ->orderByRaw("dp.chunk_kind = 'page_image'")
                        ->orderBy('dp.ordinal')
                        ->limit(self::PASSAGE_PREVIEW_LIMIT)
                        ->get();

                    $total = DB::table('silver.document_passages')
                        ->where('document_id', $report_id)
                        ->count();

                    return [$rows, (int) $total];
                },
            );
        } catch (Throwable $e) {
            $rows = collect();
            $total = 0;
        }

        return $this->passageCache[$key] = [
            'rows' => $rows->map(fn ($p) => [
                'id' => (string) $p->passage_id,
                'text' => (string) $p->text,
                'ordinal' => (int) ($p->ordinal ?? 0),
                'page_first' => $p->page_first !== null ? (int) $p->page_first : null,
                'page_last' => $p->page_last !== null ? (int) $p->page_last : null,
                'chunk_kind' => (string) ($p->chunk_kind ?? ''),
            ])->values()->all(),
            'total' => $total,
        ];
    }

    /**
     * Figure manifest — best-effort, empty when ingest hasn't extracted any.
     *
     * @return array<int, mixed>
     */
    private function figuresFor(string $report_id): array
    {
        try {
            return app(FigureResolver::class)->manifestFor($report_id);
        } catch (Throwable $e) {
            return [];
        }
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
     * NO LIVE WRITER (measured 2026-08-21).
     *
     * `silver.data_quality_flags` is written only by
     * src/dagster/georag_dagster/dq_writer.py. Dagster went dormant on
     * 2026-07-28 and has no container app in the Azure resource group, so
     * this table is empty in production and everything below returns zero.
     *
     * This is the third read-against-an-unwritten-table in this codebase:
     * SourcesController (2026-08-17) and this controller's own passage
     * lookup (2026-08-18) both joined bronze.provenance for rows the live
     * ingest path never writes. Both were found as UI regressions rather
     * than caught in review.
     *
     * src/fastapi/tests/test_data_quality_flags_have_no_live_writer.py
     * fails once a writer appears, and names this comment.
     *
     * @return array{counts: array<string, int>, open_total: int, flags: array<int, array<string, mixed>>}
     */
    private function dataQualityFlagSummary(string $reportId, string $workspaceId): array
    {
        // RLS fix 2026-08-15 (third pass): both queries below join through
        // silver.document_passages, which is fail-closed. $workspaceId is
        // resolved by the caller from the (still fail-open) silver.projects
        // row before this method runs.
        return $this->withWorkspaceRls($workspaceId, function () use ($reportId) {
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
                    'flag_id' => $f->flag_id,
                    'flag_type' => $f->flag_type,
                    'severity' => $f->severity,
                    'description' => $f->description,
                    'rule_id' => $f->rule_id,
                    'rule_version' => $f->rule_version,
                    'flagged_at' => $f->flagged_at,
                ])
                ->all();

            return [
                'counts' => $counts,
                'open_total' => array_sum($counts),
                'flags' => $flags,
            ];
        });
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
        $project = $this->resolveProject($request, $slug);

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

        return response()->json([
            'report_id' => $report_id,
            'figures' => app(FigureResolver::class)->manifestFor($report_id),
        ]);
    }

    /**
     * GET /projects/{slug}/reports/{report_id}/source
     *
     * Short-lived presigned URL for the bronze object this report was
     * parsed from, so the reader can show the original page beside the text
     * extracted from it. Answering "did OCR actually read this page
     * correctly" previously meant going and finding the PDF yourself.
     *
     * Presigned rather than proxied: these are whole NI 43-101 filings,
     * frequently hundreds of megabytes, and streaming them through Octane
     * would tie up a worker for the length of the download. The signature
     * carries its own expiry, and the membership check below is what
     * gates minting it at all.
     *
     * 404, never 403, when the report belongs to another project — the same
     * shape figures() uses, so probing cannot distinguish "not yours" from
     * "does not exist".
     */
    public function source(Request $request, string $slug, string $report_id): JsonResponse
    {
        $project = $this->resolveProject($request, $slug);

        if (! preg_match('/^[0-9a-f-]{36}$/i', $report_id)) {
            abort(404);
        }

        $row = DB::table('silver.reports')
            ->where('report_id', $report_id)
            ->where('project_id', $project->project_id)
            ->first(['source_object_key', 'title', 'page_count']);

        if (! $row) {
            abort(404, 'Report not found in this project.');
        }

        $key = $row->source_object_key ?? null;
        if (! is_string($key) || $key === '') {
            // Ingested before the key was recorded, or backfill could not
            // resolve it. Distinct from 404 so the UI can say "no original
            // stored for this document" instead of implying the document
            // itself is missing.
            return response()->json([
                'available' => false,
                'reason' => 'no_source_object_recorded',
            ], 200);
        }

        $expires = now()->addSeconds(self::SOURCE_URL_TTL_SECONDS);

        try {
            $url = app(StorageService::class)->bronzeReadOnly()
                ->temporaryUrl($key, $expires);
        } catch (Throwable $e) {
            report($e);

            return response()->json([
                'available' => false,
                'reason' => 'presign_failed',
            ], 200);
        }

        return response()->json([
            'available' => true,
            'url' => $url,
            'expires_at' => $expires->toIso8601String(),
            'filename' => (string) ($row->title ?? 'document.pdf'),
            'page_count' => isset($row->page_count) ? (int) $row->page_count : null,
        ]);
    }

    /**
     * @param mixed $raw
     *
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
