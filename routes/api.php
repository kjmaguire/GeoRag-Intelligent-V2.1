<?php

declare(strict_types=1);

use App\Http\Controllers\Api\V1\AuthController;
use App\Http\Controllers\Api\V1\ChatConversationController;
use App\Http\Controllers\Api\V1\CitationController;
use App\Http\Controllers\Api\V1\CollarController;
use App\Http\Controllers\Api\V1\ColumnMappingController;
use App\Http\Controllers\Api\V1\CoverageDensityController;
use App\Http\Controllers\Api\V1\DrillUploadController;
use App\Http\Controllers\Api\V1\ExportController;
use App\Http\Controllers\Api\V1\IngestProgressController;
use App\Http\Controllers\Api\V1\ProjectController;
use App\Http\Controllers\Api\V1\PublicApiController;
use App\Http\Controllers\Api\V1\PublicGeoscience\EntityReferencesController as PublicGeoscienceEntityReferencesController;
use App\Http\Controllers\Api\V1\QueryController;
use App\Http\Controllers\Api\V1\TrustController;
use App\Http\Controllers\Api\V1\UploadController;
use App\Http\Controllers\Api\V1\VendorProfileController;
use App\Http\Controllers\Internal\AdminSurfaceUpdatedBridgeController;
use App\Http\Controllers\Internal\IngestionProgressBroadcastController;
use App\Http\Controllers\Internal\ReportBuildProgressController;
use App\Http\Controllers\Internal\UserInboxBridgeController;
use App\Http\Controllers\Internal\WorkspaceActivityBridgeController;
use App\Http\Controllers\Internal\WorkspaceDataUpdatedBridgeController;
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
        Route::post('forgot-password', [AuthController::class, 'forgotPassword'])
            ->middleware('throttle:3,1');
        Route::post('reset-password', [AuthController::class, 'resetPassword'])
            ->middleware('throttle:5,1');
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

        // File upload — uploads to MinIO bronze bucket (triggers Dagster sensor)
        Route::post('projects/{project}/upload', [UploadController::class, 'store']);
        Route::get('upload/categories', [UploadController::class, 'categories']);

        // CC-01 Item 1 — drill-data upload: slug-routed, bronze.source_files
        // anchored, synchronous Dagster GraphQL dispatch. Distinct from the
        // generic /upload above by design — see DrillUploadController docblock.
        Route::post('projects/{slug}/drill-uploads', [DrillUploadController::class, 'store']);

        // Vendor profiles — global column-mapping profiles for parser-time field resolution
        Route::apiResource('vendor-profiles', VendorProfileController::class);
        Route::prefix('vendor-profiles/{vendor_profile}')->group(function () {
            Route::apiResource('column-mappings', ColumnMappingController::class)
                ->except(['show']);  // 'show' is redundant; use index with ?parser_type filter
        });

        // Citation resolution — looks up source text for a citation's source_chunk_id
        Route::get('citations/resolve', [CitationController::class, 'resolve']);

        // §19.2 Trust Inspector — proxy to FastAPI trust-summary endpoint.
        // Powers the per-answer-run drawer with the 7-section trust payload.
        Route::get(
            'answer-runs/{id}/trust-summary',
            [TrustController::class, 'trustSummary'],
        )->where('id', '[0-9a-fA-F-]{36}');

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

        // Public-geoscience entity references remain part of cited-answer drill-in.
        Route::prefix('public-geoscience')->group(function () {
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
});
