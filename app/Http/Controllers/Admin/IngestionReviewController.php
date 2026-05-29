<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin;

use App\Events\Admin\AdminSurfaceUpdated;
use App\Events\Admin\IngestionReviewDispositionChanged;
use App\Http\Controllers\Controller;
use App\Services\Audit\AuditEmitter;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Http\Response as HttpResponse;
use Illuminate\Support\Facades\Auth;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Event;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Master-plan §3 Step 8 — Silver Review Queue dashboard (doc-phase 58 scaffold).
 *
 * Read-only operator surface for silver.low_confidence_page_reviews:
 * shows each page the §04p quality graph routed to Silver Review with
 * its reason code, status, originating report, and per-page confidence
 * scores from silver.ocr_page_quality.
 *
 * Doc-phase 58 scope: queue list only (index action).
 * Doc-phase 59+ will add the detail panel (rendered page image,
 * parser-used breakdown, disposition controls).
 *
 * Auth: 'admin' Gate (users.is_admin = true). Admin views read across
 * all workspaces — the silver.* RLS policies have an explicit
 * "GUC unset ⇒ all rows visible" branch (see
 * database/raw/phase0/95-rls-policies.sql and the doc-phase 50
 * RLS migration).
 *
 * Routes (all under auth:sanctum):
 *   GET /admin/ingestion-review   index
 */
class IngestionReviewController extends Controller
{
    private const VALID_REASONS = [
        'ocr_confidence_below_threshold',
        'layout_confidence_below_threshold',
        'table_confidence_below_threshold',
        'rotation_undetectable',
        'deskew_failed_image_quality',
        'page_blank_or_corrupted',
        'map_heavy_v1_deferral',
        'handwriting_unparseable',
        'non_english_unsupported_language',
        'encrypted_section',
        'retry_max_exceeded',
        'other',
    ];

    private const VALID_STATUSES = [
        'pending',
        'assigned',
        'in_review',
        'resolved_accept',
        'resolved_reject',
        'resolved_reocr_requested',
    ];

    public function index(Request $request): Response
    {
        $this->authorize('admin');

        $filters = $request->validate([
            'workspace_id' => ['nullable', 'uuid'],
            'status' => ['nullable', 'in:'.implode(',', self::VALID_STATUSES)],
            'reason' => ['nullable', 'in:'.implode(',', self::VALID_REASONS)],
        ]);

        $query = DB::connection('pgsql')
            ->table('silver.low_confidence_page_reviews AS r')
            ->leftJoin('silver.reports AS rpt', 'r.report_id', '=', 'rpt.report_id')
            ->leftJoin('silver.ocr_page_quality AS pq', function ($join): void {
                $join->on('r.report_id', '=', 'pq.report_id')
                    ->on('r.page', '=', 'pq.page');
            })
            ->select([
                'r.review_item_id',
                'r.report_id',
                'r.page',
                'r.workspace_id',
                'r.reason',
                'r.status',
                'r.assigned_to',
                'r.created_at',
                'r.resolved_at',
                'rpt.title AS report_title',
                'pq.ocr_confidence',
                'pq.layout_confidence',
                'pq.table_confidence',
                'pq.parser_used',
                'pq.retry_count',
            ])
            ->orderByRaw("CASE r.status WHEN 'pending' THEN 0 WHEN 'assigned' THEN 1 WHEN 'in_review' THEN 2 ELSE 3 END")
            ->orderBy('r.created_at', 'desc')
            ->limit(200);

        if (! empty($filters['workspace_id'])) {
            $query->where('r.workspace_id', $filters['workspace_id']);
        }
        if (! empty($filters['status'])) {
            $query->where('r.status', $filters['status']);
        }
        if (! empty($filters['reason'])) {
            $query->where('r.reason', $filters['reason']);
        }

        $rows = $query->get()->map(static fn (object $row): array => [
            'review_item_id' => $row->review_item_id,
            'report_id' => $row->report_id,
            'page' => (int) $row->page,
            'workspace_id' => $row->workspace_id,
            'reason' => $row->reason,
            'status' => $row->status,
            'assigned_to' => $row->assigned_to !== null ? (int) $row->assigned_to : null,
            'created_at' => $row->created_at,
            'resolved_at' => $row->resolved_at,
            'report_title' => $row->report_title,
            'ocr_confidence' => $row->ocr_confidence !== null ? (float) $row->ocr_confidence : null,
            'layout_confidence' => $row->layout_confidence !== null ? (float) $row->layout_confidence : null,
            'table_confidence' => $row->table_confidence !== null ? (float) $row->table_confidence : null,
            'parser_used' => $row->parser_used,
            'retry_count' => $row->retry_count !== null ? (int) $row->retry_count : 0,
        ])->all();

        return Inertia::render('Admin/IngestionReview', [
            'queue' => $rows,
            'filters' => [
                'workspace_id' => $filters['workspace_id'] ?? null,
                'status' => $filters['status'] ?? null,
                'reason' => $filters['reason'] ?? null,
            ],
            'summary' => $this->summary(),
            'available_reasons' => self::VALID_REASONS,
            'available_statuses' => self::VALID_STATUSES,
        ]);
    }

    /**
     * GET /admin/ingestion-review/{review_item_id}.json
     *
     * Doc-phase 60. Returns the data the React detail panel needs:
     *   - review row + workspace + report metadata
     *   - per-page silver.ocr_page_quality
     *   - extractions (native/mixed) OR ocr_results (scanned) for the page
     *   - parser_run_artifacts for the report (retry log)
     */
    public function show(Request $request, string $reviewItemId): JsonResponse
    {
        $this->authorize('admin');

        if (preg_match('/^[0-9a-fA-F-]{36}$/', $reviewItemId) !== 1) {
            abort(404);
        }

        $review = DB::connection('pgsql')
            ->table('silver.low_confidence_page_reviews AS r')
            ->leftJoin('silver.reports AS rpt', 'r.report_id', '=', 'rpt.report_id')
            ->leftJoin('silver.ocr_page_quality AS pq', function ($join): void {
                $join->on('r.report_id', '=', 'pq.report_id')
                    ->on('r.page', '=', 'pq.page');
            })
            ->where('r.review_item_id', $reviewItemId)
            ->select([
                'r.review_item_id',
                'r.report_id',
                'r.page',
                'r.workspace_id',
                'r.reason',
                'r.status',
                'r.assigned_to',
                'r.created_at',
                'r.resolved_at',
                'r.resolution_notes',
                'rpt.title AS report_title',
                'rpt.company AS report_company',
                'rpt.filing_date AS report_filing_date',
                'pq.ocr_confidence',
                'pq.layout_confidence',
                'pq.table_confidence',
                'pq.parser_used',
                'pq.retry_count',
                'pq.deskew_applied',
                'pq.rotation_applied',
            ])
            ->first();

        if ($review === null) {
            abort(404);
        }

        $extractions = DB::connection('pgsql')
            ->table('silver.ingest_extractions')
            ->where('report_id', $review->report_id)
            ->where('page', $review->page)
            ->orderBy('region')
            ->limit(200)
            ->get(['region', 'bbox', 'source_method', 'extraction_confidence', 'text_content', 'payload']);

        $ocrResults = DB::connection('pgsql')
            ->table('silver.ingest_ocr_results')
            ->where('report_id', $review->report_id)
            ->where('page', $review->page)
            ->orderBy('region')
            ->limit(200)
            ->get(['region', 'bbox', 'source_method', 'extraction_confidence', 'ocr_text', 'language_hint', 'payload']);

        $layouts = DB::connection('pgsql')
            ->table('silver.ingest_layouts')
            ->where('report_id', $review->report_id)
            ->where('page', $review->page)
            ->orderBy('region')
            ->limit(200)
            ->get(['region', 'bbox', 'source_method', 'extraction_confidence', 'layout_label', 'payload']);

        $parserRuns = DB::connection('pgsql')
            ->table('silver.parser_run_artifacts')
            ->where('report_id', $review->report_id)
            ->orderBy('started_at')
            ->get([
                'run_id',
                'parser_used',
                'parser_version',
                'raw_output_uri',
                'errors',
                'warnings',
                'started_at',
                'finished_at',
            ]);

        $docQuality = DB::connection('pgsql')
            ->table('silver.document_ingestion_quality')
            ->where('report_id', $review->report_id)
            ->first(['total_pages', 'low_confidence_pages', 'overall_quality_score', 'recommended_action']);

        return response()->json([
            'review' => [
                'review_item_id' => $review->review_item_id,
                'report_id' => $review->report_id,
                'page' => (int) $review->page,
                'workspace_id' => $review->workspace_id,
                'reason' => $review->reason,
                'status' => $review->status,
                'assigned_to' => $review->assigned_to !== null ? (int) $review->assigned_to : null,
                'created_at' => $review->created_at,
                'resolved_at' => $review->resolved_at,
                'resolution_notes' => $review->resolution_notes,
            ],
            'report' => [
                'report_id' => $review->report_id,
                'title' => $review->report_title,
                'company' => $review->report_company,
                'filing_date' => $review->report_filing_date,
            ],
            'page_quality' => [
                'ocr_confidence' => $review->ocr_confidence !== null ? (float) $review->ocr_confidence : null,
                'layout_confidence' => $review->layout_confidence !== null ? (float) $review->layout_confidence : null,
                'table_confidence' => $review->table_confidence !== null ? (float) $review->table_confidence : null,
                'parser_used' => $review->parser_used,
                'retry_count' => $review->retry_count !== null ? (int) $review->retry_count : 0,
                'deskew_applied' => (bool) ($review->deskew_applied ?? false),
                'rotation_applied' => $review->rotation_applied !== null ? (float) $review->rotation_applied : null,
            ],
            'document_quality' => $docQuality === null ? null : [
                'total_pages' => (int) $docQuality->total_pages,
                'low_confidence_pages' => (int) $docQuality->low_confidence_pages,
                'overall_quality_score' => $docQuality->overall_quality_score !== null
                    ? (float) $docQuality->overall_quality_score : null,
                'recommended_action' => $docQuality->recommended_action,
            ],
            'extractions' => $extractions->map(static fn (object $r): array => [
                'region' => (int) $r->region,
                'bbox' => $r->bbox,
                'source_method' => $r->source_method,
                'extraction_confidence' => $r->extraction_confidence !== null
                    ? (float) $r->extraction_confidence : null,
                'text_content' => $r->text_content,
                'payload' => is_string($r->payload) ? json_decode($r->payload, true) : $r->payload,
            ])->all(),
            'ocr_results' => $ocrResults->map(static fn (object $r): array => [
                'region' => (int) $r->region,
                'bbox' => $r->bbox,
                'source_method' => $r->source_method,
                'extraction_confidence' => $r->extraction_confidence !== null
                    ? (float) $r->extraction_confidence : null,
                'ocr_text' => $r->ocr_text,
                'language_hint' => $r->language_hint,
                'payload' => is_string($r->payload) ? json_decode($r->payload, true) : $r->payload,
            ])->all(),
            'layouts' => $layouts->map(static fn (object $r): array => [
                'region' => (int) $r->region,
                'bbox' => $r->bbox,
                'source_method' => $r->source_method,
                'extraction_confidence' => $r->extraction_confidence !== null
                    ? (float) $r->extraction_confidence : null,
                'layout_label' => $r->layout_label,
                'payload' => is_string($r->payload) ? json_decode($r->payload, true) : $r->payload,
            ])->all(),
            'parser_runs' => $parserRuns->map(static fn (object $r): array => [
                'run_id' => $r->run_id,
                'parser_used' => $r->parser_used,
                'parser_version' => $r->parser_version,
                'raw_output_uri' => $r->raw_output_uri,
                'errors' => is_string($r->errors) ? json_decode($r->errors, true) : $r->errors,
                'warnings' => is_string($r->warnings) ? json_decode($r->warnings, true) : $r->warnings,
                'started_at' => $r->started_at,
                'finished_at' => $r->finished_at,
            ])->all(),
            'page_render_url' => route('admin.ingestion-review.page-render', [
                'review_item_id' => $reviewItemId,
                'page' => $review->page,
            ]),
        ]);
    }

    /**
     * PATCH /admin/ingestion-review/{review_item_id}
     *
     * Doc-phase 61 / master-plan §3 Step 8d. Apply an operator's
     * disposition decision to a review item. Validates the target
     * status against the CHECK enum + enforces "resolved-is-terminal"
     * at the application layer (the DB lets you transition out of
     * resolved_* but the product semantic says they're final).
     */
    public function update(Request $request, string $reviewItemId): JsonResponse
    {
        $this->authorize('admin');

        if (preg_match('/^[0-9a-fA-F-]{36}$/', $reviewItemId) !== 1) {
            abort(404);
        }

        $validated = $request->validate([
            'status' => ['required', 'in:'.implode(',', self::VALID_STATUSES)],
            'resolution_notes' => ['nullable', 'string', 'max:4000'],
        ]);

        $current = DB::connection('pgsql')
            ->table('silver.low_confidence_page_reviews')
            ->where('review_item_id', $reviewItemId)
            ->first(['status', 'resolved_at']);

        if ($current === null) {
            abort(404);
        }

        // Resolved-is-terminal: reject transitions out of resolved_*
        if (str_starts_with($current->status, 'resolved_')
            && $validated['status'] !== $current->status) {
            return response()->json([
                'error' => 'resolved review items are terminal — cannot transition out',
                'current_status' => $current->status,
            ], 422);
        }

        $newStatus = $validated['status'];
        $isResolved = str_starts_with($newStatus, 'resolved_');
        $userId = Auth::id();

        $previousStatus = $current->status;

        DB::connection('pgsql')->transaction(function () use (
            $reviewItemId, $newStatus, $validated, $isResolved, $userId, $previousStatus
        ): void {
            DB::connection('pgsql')
                ->table('silver.low_confidence_page_reviews')
                ->where('review_item_id', $reviewItemId)
                ->update([
                    'status' => $newStatus,
                    'resolution_notes' => $validated['resolution_notes'] ?? null,
                    'resolved_at' => $isResolved ? now() : null,
                    'assigned_to' => $isResolved ? $userId : null,
                ]);

            // Doc-phase 64 — audit emit inside the same transaction so
            // the row write + audit are atomically committed together.
            try {
                app(AuditEmitter::class)->emit(
                    actionType: 'silver.low_confidence_page_reviews.disposition',
                    actorId: $userId,
                    actorKind: AuditEmitter::ACTOR_USER,
                    targetSchema: 'silver',
                    targetTable: 'low_confidence_page_reviews',
                    targetId: $reviewItemId,
                    payload: [
                        'previous_status' => $previousStatus,
                        'new_status' => $newStatus,
                        'is_resolved' => $isResolved,
                        'has_notes' => ! empty($validated['resolution_notes']),
                    ],
                );
            } catch (\Throwable $e) {
                Log::warning('IngestionReview: audit emit failed', [
                    'review_item_id' => $reviewItemId,
                    'error' => $e->getMessage(),
                ]);
            }
        });

        // Doc-phase 63 — on resolved_reocr_requested, dispatch the
        // Hatchet re_ocr_page workflow via FastAPI's internal trigger.
        // Wrapped in try/catch so a downstream Hatchet outage doesn't
        // block the disposition write (the operator's decision is
        // already persisted above).
        $reOcrTriggered = false;
        $reOcrError = null;
        if ($newStatus === 'resolved_reocr_requested') {
            try {
                $reOcrTriggered = $this->dispatchReOcr($reviewItemId, $userId);
            } catch (\Throwable $e) {
                $reOcrError = $e->getMessage();
                Log::warning(
                    'IngestionReview: re-OCR trigger failed',
                    [
                        'review_item_id' => $reviewItemId,
                        'error' => $reOcrError,
                    ],
                );
            }
        }

        // Doc-phase 64 — Reverb broadcast for multi-operator queue sync.
        // Wrapped in try/catch so a broadcasting outage doesn't fail the
        // disposition write (already committed above).
        try {
            $row = DB::connection('pgsql')
                ->table('silver.low_confidence_page_reviews')
                ->where('review_item_id', $reviewItemId)
                ->first(['report_id', 'page', 'reason']);
            if ($row !== null) {
                Event::dispatch(new IngestionReviewDispositionChanged(
                    reviewItemId: $reviewItemId,
                    reportId: $row->report_id,
                    page: (int) $row->page,
                    newStatus: $newStatus,
                    reason: $row->reason,
                    actorId: $userId,
                    reOcrTriggered: $reOcrTriggered,
                ));

                // Phase 2 — also fire the generic admin surface event so
                // the page's useAdminSurfaceUpdated hook triggers a partial
                // reload of {queue, summary}. The detail-row patching
                // (IngestionReviewDispositionChanged) and the list-level
                // refresh (AdminSurfaceUpdated) are complementary — the
                // first updates an already-rendered row in place, the
                // second pulls fresh counts and any newly-arrived rows.
                AdminSurfaceUpdated::dispatch(
                    'ingestion-review',
                    null,
                    ['queue', 'summary'],
                    [
                        'review_item_id' => $reviewItemId,
                        'new_status' => $newStatus,
                        'actor_id' => $userId,
                    ],
                );
            }
        } catch (\Throwable $e) {
            Log::warning('IngestionReview: Reverb broadcast failed', [
                'review_item_id' => $reviewItemId,
                'error' => $e->getMessage(),
            ]);
        }

        return response()->json([
            'review_item_id' => $reviewItemId,
            'status' => $newStatus,
            'resolution_notes' => $validated['resolution_notes'] ?? null,
            'resolved_at' => $isResolved ? now()->toIso8601String() : null,
            're_ocr_triggered' => $reOcrTriggered,
            're_ocr_error' => $reOcrError,
        ]);
    }

    /**
     * Dispatch the re_ocr_page Hatchet workflow via FastAPI's internal
     * trigger endpoint. Returns true on accepted (HTTP 202), false on
     * any non-2xx upstream response. Throws on connect/timeout — caller
     * wraps in try/catch.
     */
    private function dispatchReOcr(string $reviewItemId, ?int $actorId): bool
    {
        $row = DB::connection('pgsql')
            ->table('silver.low_confidence_page_reviews')
            ->where('review_item_id', $reviewItemId)
            ->first(['report_id', 'page', 'workspace_id']);

        if ($row === null) {
            return false;
        }

        $fastapiBase = rtrim(
            config('services.fastapi.url')
                ?? env('FASTAPI_BASE_URL', 'http://fastapi:8000'),
            '/',
        );
        $serviceKey = env('FASTAPI_SERVICE_KEY');
        if (! $serviceKey) {
            throw new \RuntimeException('FASTAPI_SERVICE_KEY not configured');
        }

        $response = Http::withHeaders(['X-Service-Key' => $serviceKey])
            ->timeout(10)
            ->post($fastapiBase.'/internal/v1/re_ocr_page/trigger', [
                'workspace_id' => $row->workspace_id,
                'report_id' => $row->report_id,
                'page' => (int) $row->page,
                'review_item_id' => $reviewItemId,
                'actor_id' => $actorId,
            ]);

        return $response->status() === 202;
    }

    /**
     * GET /admin/ingestion-review/{review_item_id}/page/{page}.png
     *
     * Doc-phase 60. Reverse-proxies the FastAPI render endpoint with the
     * X-Service-Key header attached server-side. Keeps the service key
     * out of the browser.
     */
    public function pageRender(Request $request, string $reviewItemId, int $page): HttpResponse
    {
        $this->authorize('admin');

        if (preg_match('/^[0-9a-fA-F-]{36}$/', $reviewItemId) !== 1) {
            abort(404);
        }

        // Look up report_id from review_item_id.
        $reportId = DB::connection('pgsql')
            ->table('silver.low_confidence_page_reviews')
            ->where('review_item_id', $reviewItemId)
            ->value('report_id');

        if ($reportId === null) {
            abort(404);
        }

        $scale = (float) $request->query('scale', '2.0');
        if ($scale <= 0.0 || $scale > 10.0) {
            $scale = 2.0;
        }

        $fastapiBase = rtrim(
            config('services.fastapi.url')
                ?? env('FASTAPI_BASE_URL', 'http://fastapi:8000'),
            '/',
        );
        $serviceKey = env('FASTAPI_SERVICE_KEY');
        if (! $serviceKey) {
            abort(500, 'FASTAPI_SERVICE_KEY not configured');
        }

        $response = Http::withHeaders(['X-Service-Key' => $serviceKey])
            ->timeout(30)
            ->get($fastapiBase.'/internal/v1/ocr/render', [
                'report_id' => $reportId,
                'page' => $page,
                'scale' => $scale,
            ]);

        if ($response->failed()) {
            // 404 from upstream → 404 here; other failures → 502.
            $status = $response->status();
            if ($status === 404) {
                abort(404, 'page render unavailable');
            }
            abort(502, 'upstream render failed: HTTP '.$status);
        }

        return response($response->body(), 200, [
            'Content-Type' => 'image/png',
            'Cache-Control' => 'private, max-age=300',
        ]);
    }

    /**
     * @return array{
     *     by_status: array<string, int>,
     *     by_reason: array<string, int>,
     *     total_pending: int,
     *     last_24h_new: int,
     * }
     */
    private function summary(): array
    {
        $byStatus = DB::connection('pgsql')
            ->table('silver.low_confidence_page_reviews')
            ->select('status', DB::raw('count(*) AS n'))
            ->groupBy('status')
            ->pluck('n', 'status')
            ->map(static fn ($n) => (int) $n)
            ->all();

        $byReason = DB::connection('pgsql')
            ->table('silver.low_confidence_page_reviews')
            ->select('reason', DB::raw('count(*) AS n'))
            ->where('status', 'pending')
            ->groupBy('reason')
            ->pluck('n', 'reason')
            ->map(static fn ($n) => (int) $n)
            ->all();

        $last24h = (int) DB::connection('pgsql')
            ->table('silver.low_confidence_page_reviews')
            ->where('created_at', '>=', now()->subDay())
            ->count();

        return [
            'by_status' => $byStatus,
            'by_reason' => $byReason,
            'total_pending' => $byStatus['pending'] ?? 0,
            'last_24h_new' => $last24h,
        ];
    }
}
