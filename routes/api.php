<?php

declare(strict_types=1);

use App\Http\Controllers\Api\V1\AuthController;
use App\Http\Controllers\Api\V1\ChatConversationController;
use App\Http\Controllers\Api\V1\CitationController;
use App\Http\Controllers\Api\V1\CollarController;
use App\Http\Controllers\Api\V1\ColumnMappingController;
use App\Http\Controllers\Api\V1\CoverageDensityController;
use App\Http\Controllers\Api\V1\DrillUploadController;
use App\Http\Controllers\Api\V1\EvidenceController;
use App\Http\Controllers\Api\V1\ExportController;
use App\Http\Controllers\Api\V1\HoleAnalysisController;
use App\Http\Controllers\Api\V1\IngestProgressController;
use App\Http\Controllers\Api\V1\ProjectController;
use App\Http\Controllers\Api\V1\PublicApiController;
use App\Http\Controllers\Api\V1\PublicGeoscience\EntityReferencesController as PublicGeoscienceEntityReferencesController;
use App\Http\Controllers\Api\V1\PublicGeoscience\FeatureDetailController as PublicGeoscienceFeatureDetailController;
use App\Http\Controllers\Api\V1\PublicGeoscience\HealthController as PublicGeoscienceHealthController;
use App\Http\Controllers\Api\V1\PublicGeoscience\JurisdictionController as PublicGeoscienceJurisdictionController;
use App\Http\Controllers\Api\V1\QueryController;
use App\Http\Controllers\Api\V1\SavedMapViewController;
use App\Http\Controllers\Api\V1\TrustController;
use App\Http\Controllers\Api\V1\UploadController;
use App\Http\Controllers\Api\V1\VendorProfileController;
use App\Http\Controllers\ChartsGalleryController;
use App\Http\Controllers\Dashboard\PortfolioController;
use App\Http\Controllers\Dashboard\ProjectAnalyticsController;
use App\Http\Controllers\Dashboard\ProjectDashboardController;
use App\Http\Controllers\Internal\AdminSurfaceUpdatedBridgeController;
use App\Http\Controllers\Internal\IngestionProgressBroadcastController;
use App\Http\Controllers\Internal\PublicGeoscienceTilesInvalidatedBridgeController;
use App\Http\Controllers\Internal\ReportBuildProgressController;
use App\Http\Controllers\Internal\UserInboxBridgeController;
use App\Http\Controllers\Internal\WorkspaceActivityBridgeController;
use App\Http\Controllers\Internal\WorkspaceDataUpdatedBridgeController;
use App\Http\Controllers\InterpretationWorkspaceController;
use Illuminate\Support\Facades\Route;

/*
|--------------------------------------------------------------------------
| GeoRAG API Routes — v1
|--------------------------------------------------------------------------
|
| All routes are prefixed with /api (Laravel's default ApiServiceProvider
| binding) and versioned under /v1 via the group below.
|
| Authentication is handled by Laravel Sanctum. Public routes (auth
| endpoints) are outside the auth:sanctum middleware group. All data
| endpoints require a valid Bearer token.
|
*/

Route::prefix('v1')->group(function () {
    // ── Public routes (no auth required) ─────────────────────────────────
    // Named `auth-login` limiter (AppServiceProvider::boot) keys on
    // email + IP, so /login and /spa-login share a single 5/min bucket
    // per credential-and-origin pair — a single attacker can't split the
    // budget across two endpoints, and shared-NAT users don't throttle
    // each other. Register stays on its own tighter IP-keyed throttle
    // because we don't want anonymous account-enumeration to benefit from
    // per-email keys.
    Route::prefix('auth')->group(function () {
        Route::post('register', [AuthController::class, 'register'])
            ->middleware('throttle:3,1');
        Route::post('login', [AuthController::class, 'login'])
            ->middleware('throttle:auth-login');
        // SPA cookie-based login — no token returned, session cookie is the credential.
        // Client must first GET /sanctum/csrf-cookie to prime XSRF-TOKEN.
        Route::post('spa-login', [AuthController::class, 'spaLogin'])
            ->middleware('throttle:auth-login');
    });

    // ── Protected routes (require valid Sanctum token) ───────────────────
    Route::middleware('auth:sanctum')->group(function () {
        // Auth — logout + profile
        Route::post('auth/logout', [AuthController::class, 'logout']);
        Route::get('auth/me', [AuthController::class, 'me']);

        // Reliability spec Phase 4 — per-run polling fallback for
        // silver.ingest_progress. Returns 404 (not 403) on cross-workspace
        // run_ids so an attacker can't fingerprint existence.
        Route::get('ingest-progress/{run_id}', [IngestProgressController::class, 'show'])
            ->where('run_id', '[0-9a-f-]{36}')
            ->name('ingest_progress.show');

        // Projects — full CRUD (scoped to user's memberships in controller)
        Route::apiResource('projects', ProjectController::class);

        // Collars — scoped to a project (nested resource)
        Route::apiResource('projects.collars', CollarController::class)
            ->scoped()
            ->only(['index', 'store', 'show', 'destroy']);

        // Saved map views — per-user-per-project MapLibre state (§6.5 doc-phase 105 skeleton).
        // Controller methods currently throw LogicException; live behavior
        // lands when the §6.7+ MapLibre frontend calls these endpoints.
        Route::apiResource('projects.saved-map-views', SavedMapViewController::class)
            ->scoped()
            ->parameters(['saved-map-views' => 'view']);

        // Per-hole geological analysis: surveys + structures + geochemistry
        // in one payload for the Explorer "Analysis" tab.
        Route::get('projects/{projectId}/holes/{holeIdOrCollarId}/analysis', [HoleAnalysisController::class, 'show']);

        // CC-03 Item 5 — coverage density GeoJSON for the MapView heatmap layer.
        Route::get('projects/{projectId}/coverage-density', [CoverageDensityController::class, 'show'])
            ->where('projectId', '[0-9a-f-]{36}');

        // RAG query — two-phase subscribe-ACK handshake so the client is
        // guaranteed to be on the Echo channel before the Horizon job
        // starts broadcasting. See QueryController docblock.
        //
        // Shared `queries` limiter (AppServiceProvider::boot) keys on the
        // authenticated user and counts BOTH phases together. Without the
        // shared bucket the previous `throttle:30,1` was charged twice per
        // logical query (once on reserve, once on dispatch), halving the
        // real capacity to ~15/min.
        Route::post('queries', [QueryController::class, 'store'])
            ->middleware('throttle:queries');
        Route::post('queries/{queryId}/start', [QueryController::class, 'start'])
            ->middleware('throttle:queries')
            ->where('queryId', '[0-9a-f-]{36}');

        // Chat history sync (localStorage-first, durable server-side store).
        Route::get('conversations', [ChatConversationController::class, 'index']);
        Route::get('conversations/{conversationId}', [ChatConversationController::class, 'show'])
            ->where('conversationId', '[0-9a-f-]{36}');
        Route::put('conversations/{conversationId}', [ChatConversationController::class, 'upsert'])
            ->where('conversationId', '[0-9a-f-]{36}');
        Route::delete('conversations/{conversationId}', [ChatConversationController::class, 'destroy'])
            ->where('conversationId', '[0-9a-f-]{36}');

        // Exports — scoped to a project; dispatch Horizon jobs, poll status
        Route::apiResource('projects.exports', ExportController::class)
            ->scoped()
            ->only(['index', 'store', 'show'])
            ->names([
                'index' => 'api.projects.exports.index',
                'store' => 'api.projects.exports.store',
                'show' => 'api.projects.exports.show',
            ]);

        // Download redirect — not scoped under project so clients can bookmark it
        Route::get('exports/{export}/download', [ExportController::class, 'download'])
            ->name('exports.download');

        // File upload — uploads to MinIO bronze bucket (triggers Dagster sensor).
        // throttle:uploads (audit item F) — 200/hr per workspace. The dispatch-
        // side HatchetDispatchThrottle still applies its own per-workspace cap
        // downstream; this is the EDGE limiter that stops the user from queueing
        // ten thousand uploads before they hit the dispatch layer.
        Route::post('projects/{project}/upload', [UploadController::class, 'store'])
            ->middleware('throttle:uploads');
        Route::get('upload/categories', [UploadController::class, 'categories']);

        // CC-01 Item 1 — drill-data upload: slug-routed, bronze.source_files
        // anchored, synchronous Dagster GraphQL dispatch. Distinct from the
        // generic /upload above by design — see DrillUploadController docblock.
        // Same throttle:uploads bucket so a 200-file drill upload doesn't share
        // budget separately from a parallel generic-upload session.
        Route::post('projects/{slug}/drill-uploads', [DrillUploadController::class, 'store'])
            ->middleware('throttle:uploads');

        // Vendor profiles — global column-mapping profiles for parser-time field resolution
        Route::apiResource('vendor-profiles', VendorProfileController::class);
        Route::prefix('vendor-profiles/{vendor_profile}')->group(function () {
            Route::apiResource('column-mappings', ColumnMappingController::class)
                ->except(['show']);  // 'show' is redundant; use index with ?parser_type filter
        });

        // Citation resolution — looks up source text for a citation's source_chunk_id
        Route::get('citations/resolve', [CitationController::class, 'resolve']);

        // Evidence Inspector — Laravel proxy for FastAPI /v1/evidence/{id}.
        // Added 2026-06-03 to close the VITE_SERVICE_KEY leak vector
        // (see EvidenceController docblock + AUDIT_AND_FIX_REPORT.md
        // Theme K). The proxy resolves the evidence_item's project,
        // gates on hasProjectAccess, mints the FastAPI JWT, injects
        // the service key, and forwards.
        Route::get('evidence/{evidenceId}', [EvidenceController::class, 'show']);

        // §19.2 Trust Inspector — proxy to FastAPI trust-summary endpoint.
        // Powers the per-answer-run drawer with the 7-section trust payload.
        Route::get(
            'answer-runs/{id}/trust-summary',
            [TrustController::class, 'trustSummary'],
        )->where('id', '[0-9a-fA-F-]{36}');

        // §19.3 Interpretation Workspace — proxy GET/POST/PUT/DELETE for
        // notes / section-lines / target-zones / comments. Catch-all tail.
        Route::match(
            ['get', 'post', 'put', 'delete'],
            'interpretation/{tail}',
            [InterpretationWorkspaceController::class, 'proxy'],
        )->where('tail', '.*');

        // §17.3 Charts Gallery — render any of the 8 chart kinds.
        // throttle:charts (audit item F) — 60/min per workspace. Renders are
        // interactive and bursty; the limit is sized to absorb 3 concurrent
        // operators per workspace while shutting down a runaway re-render loop
        // inside a minute.
        Route::post(
            'charts/render',
            [ChartsGalleryController::class, 'render'],
        )->middleware('throttle:charts');

        // §3.3 Public REST API breadth — 8 endpoint groups + self-describing index.
        Route::get('', [PublicApiController::class, 'index']);
        Route::get('openapi.json', [PublicApiController::class, 'openapi']);
        Route::get('answers/{answer_run_id}', [PublicApiController::class, 'answer'])->where('answer_run_id', '[0-9a-fA-F-]{36}');
        Route::get('maps/{project_id}/layers', [PublicApiController::class, 'mapLayers'])->where('project_id', '[0-9a-fA-F-]{36}');
        Route::get('reports', [PublicApiController::class, 'reports']);
        Route::get('targets/{project_id}', [PublicApiController::class, 'targets'])->where('project_id', '[0-9a-fA-F-]{36}');
        Route::get('interpretations/{project_id}', [PublicApiController::class, 'interpretations'])->where('project_id', '[0-9a-fA-F-]{36}');
        Route::get('audit/{workspace_id}', [PublicApiController::class, 'audit'])->where('workspace_id', '[0-9a-fA-F-]{36}');
        Route::get('usage/{workspace_id}', [PublicApiController::class, 'usage'])->where('workspace_id', '[0-9a-fA-F-]{36}');
        Route::get('webhooks', [PublicApiController::class, 'webhooks']);

        // ── Public Geoscience (§10) — read-only jurisdiction registry ──
        Route::prefix('public-geoscience')->group(function () {
            Route::get('jurisdictions', [PublicGeoscienceJurisdictionController::class, 'index']);
            Route::get('health', PublicGeoscienceHealthController::class);

            // Single-feature detail fetch — backs the in-map "Expand
            // upstream record" panel + the Compare-Features modal.
            // Layer ID validated server-side (LAYER_TABLES registry).
            // Feature ID is the source agency's identifier (e.g. SMDI
            // number, MINFILE code) or our canonical source_feature_id.
            Route::get(
                'features/{layer}/{feature_id}',
                [PublicGeoscienceFeatureDetailController::class, 'show'],
            )->where('layer', '[a-z_]+')
                ->where('feature_id', '[A-Za-z0-9._\-]+');

            // Cross-corpus linker drill-in (plan §07d).
            // GET .../entities/{canonical_type}/{pg_id}/references
            // GET .../documents/{report_id}/references
            Route::get(
                'entities/{canonical_type}/{pg_id}/references',
                [PublicGeoscienceEntityReferencesController::class, 'forEntity'],
            )->where('canonical_type', 'mine|mineral_occurrence|drillhole_collar|resource_potential_zone|rock_sample|assessment_survey|mineral_disposition');
            Route::get(
                'documents/{report_id}/references',
                [PublicGeoscienceEntityReferencesController::class, 'forDocument'],
            );
        });

        // ── Dashboard API endpoints (§3–§4 of dashboard spec) ───────────
        Route::prefix('dashboard')->group(function () {
            Route::get('platform-readiness', [PortfolioController::class, 'platformReadiness']);
            Route::get('portfolio/kpis', [PortfolioController::class, 'kpis']);
            Route::get('portfolio/projects', [PortfolioController::class, 'projects']);
            Route::get('portfolio/query-activity', [PortfolioController::class, 'queryActivity']);
            Route::get('portfolio/ingestion-health', [PortfolioController::class, 'ingestionHealth']);
            Route::get('portfolio/feedback', [PortfolioController::class, 'feedback']);
            Route::get('portfolio/activity', [PortfolioController::class, 'activity']);

            Route::get('projects/{slug}/header', [ProjectDashboardController::class, 'header']);
            Route::get('projects/{slug}/kpis', [ProjectDashboardController::class, 'kpis']);
            Route::get('projects/{slug}/aoi', [ProjectDashboardController::class, 'aoi']);
            Route::get('projects/{slug}/kg-counts', [ProjectDashboardController::class, 'kgCounts']);
            Route::get('projects/{slug}/recent-queries', [ProjectDashboardController::class, 'recentQueries']);
            Route::get('projects/{slug}/feedback', [ProjectDashboardController::class, 'feedback']);
            Route::get('projects/{slug}/documents', [ProjectDashboardController::class, 'documents']);
            Route::get('projects/{slug}/drill-summary', [ProjectDashboardController::class, 'drillSummary']);
            Route::get('projects/{slug}/analytics', [ProjectAnalyticsController::class, 'show']);

            // D6 — project context banner. Keyed on UUID so the chat page
            // (which holds project_id but not slug) can surface a one-line
            // "Project: Lazy Edward Bay · Uranium · Saskatchewan · NAD83 zone 14N · 20 holes"
            // header above the input. Response shape is a flat dict tuned
            // for rendering; expensive aggregates are elided.
            Route::get('projects/by-id/{projectId}/context', [ProjectDashboardController::class, 'context']);
        });
    });
});

/*
|--------------------------------------------------------------------------
| Internal — FastAPI → Laravel callback bridge
|--------------------------------------------------------------------------
| Service-key auth only (FASTAPI_SERVICE_KEY shared secret). These routes
| let FastAPI push events into Laravel for fan-out via Reverb (real-time
| progress without long polling).
*/
Route::middleware('service.key')->prefix('internal')->group(function () {
    Route::post('admin/reports/{build_id}/progress',
        [ReportBuildProgressController::class, 'broadcast'])
        ->middleware('throttle:bridge:report-progress')
        ->where('build_id', '[0-9a-f-]{36}')
        ->name('internal.reports.progress');

    // Reliability spec Phase 1 — FastAPI on_failure_task / stale_run_sweep /
    // embed_verify post here so Laravel can broadcast ingestion.progress
    // events on project.{projectId}.ingestion private channels.
    Route::post('v1/ingest-progress/broadcast',
        [IngestionProgressBroadcastController::class, 'broadcast'])
        ->name('internal.ingest_progress.broadcast');

    // Non-ingestion workspace updates — score_targets and other
    // project-scoped workflows whose completion writes tables the SPA
    // reads directly (no MV refresh, no data_version bump). Reuses the
    // existing project.{projectId}.ingestion private channel and the
    // WorkspaceDataUpdated event so the existing useWorkspaceDataUpdated
    // hook on the receiving page handles the partial reload.
    Route::post('v1/workspace-data-updated',
        [WorkspaceDataUpdatedBridgeController::class, 'broadcast'])
        ->name('internal.workspace_data_updated.broadcast');

    // Phase 2 admin surface push — generic bridge for the 10 admin pages
    // that need real-time reloads. Workflow / agent code POSTs with a
    // {surface, surface_id?, affected_props[], payload?} body; the
    // controller validates the surface against the channel registry in
    // routes/channels.php and dispatches App\Events\Admin\AdminSurfaceUpdated.
    Route::post('v1/admin-surface-updated',
        [AdminSurfaceUpdatedBridgeController::class, 'broadcast'])
        ->name('internal.admin_surface_updated.broadcast');

    // Phase 3 — workspace-level activity push for Foundry/Portfolio +
    // Foundry/Projects. Caller POSTs {workspace_id, affected_types[],
    // payload?}; dispatches App\Events\Workspace\WorkspaceActivityBroadcast
    // on workspace.{workspace_id}.activity (channel was registered for
    // dashboard spec §6 but never used by a writer before Phase 3).
    Route::post('v1/workspace-activity',
        [WorkspaceActivityBridgeController::class, 'broadcast'])
        ->name('internal.workspace_activity.broadcast');

    // Phase 3 — per-user inbox push for Foundry/Inbox + nav-bar badge.
    // Caller POSTs {user_id, kind in (mention|review|refusal), count_delta?,
    // payload?}; dispatches App\Events\User\UserInboxUpdated on the
    // Laravel-default App.Models.User.{user_id} private channel.
    Route::post('v1/user-inbox-updated',
        [UserInboxBridgeController::class, 'broadcast'])
        ->name('internal.user_inbox_updated.broadcast');

    // Phase 4 — Public-Geoscience tile cache invalidation. Caller POSTs
    // {jurisdiction_epoch, source_ids?}; dispatches
    // App\Events\Map\PublicGeoscienceTilesInvalidated so PublicGeoscienceMap
    // re-issues setTiles() with the new ?v={epoch} cache-bust.
    Route::post('v1/public-geoscience-tiles-invalidated',
        [PublicGeoscienceTilesInvalidatedBridgeController::class, 'broadcast'])
        ->name('internal.public_geoscience_tiles_invalidated.broadcast');
});
