<?php

declare(strict_types=1);

use App\Http\Controllers\Admin\AdminMiscController;
use App\Http\Controllers\Admin\AgentConfig\PinsController as AdminAgentConfigPins;
use App\Http\Controllers\Admin\AgentConfig\PromptsController as AdminAgentConfigPrompts;
use App\Http\Controllers\Admin\AgentConfig\TimeoutsController as AdminAgentConfigTimeouts;
use App\Http\Controllers\Admin\AgentConfig\WorkspacesController as AdminAgentConfigWorkspaces;
use App\Http\Controllers\Admin\AuditFindingsController;
use App\Http\Controllers\Admin\CacheTelemetryController;
use App\Http\Controllers\Admin\ClusterIngestController;
use App\Http\Controllers\Admin\ConflictsController;
use App\Http\Controllers\Admin\DashboardsController as AdminDashboardsController;
use App\Http\Controllers\Admin\DecisionHistoryController;
use App\Http\Controllers\Admin\EvalCompareController;
use App\Http\Controllers\Admin\EvalDashboardController;
use App\Http\Controllers\Admin\EvalQuestionsController;
use App\Http\Controllers\Admin\HatchetWorkersController;
use App\Http\Controllers\Admin\HypothesisWorkspaceController;
// Phase 4 Step 6 — ShadowRunsController removed alongside silver.shadow_runs.
use App\Http\Controllers\Admin\IngestionReviewController;
use App\Http\Controllers\Admin\IntegrationsController;
use App\Http\Controllers\Admin\KestraSsoController;
use App\Http\Controllers\Admin\MlTrainingRunsController;
use App\Http\Controllers\Admin\ReportBuilderController;
use App\Http\Controllers\Admin\SupportCockpitController;
use App\Http\Controllers\Admin\TargetRecommendationCockpitController;
use App\Http\Controllers\Admin\Tier234Controller;
use App\Http\Controllers\Admin\WhatChangedController;
use App\Http\Controllers\Admin\WorkflowRunController;
use App\Http\Controllers\ChartsGalleryController;
use App\Http\Controllers\CitationFeedbackController;
use App\Http\Controllers\Dashboard\CustomerDashboardsController;
use App\Http\Controllers\Foundry\AssessmentSummaryController;
use App\Http\Controllers\Foundry\AuditLogController;
use App\Http\Controllers\Foundry\ChatController;
use App\Http\Controllers\Foundry\CorpusController;
use App\Http\Controllers\Foundry\DecisionsController;
use App\Http\Controllers\Foundry\DrillholeDetailController;
use App\Http\Controllers\Foundry\DrillReviewController;
use App\Http\Controllers\Foundry\ExplorerController;
use App\Http\Controllers\Foundry\HoleCompareController;
use App\Http\Controllers\Foundry\InboxController;
use App\Http\Controllers\Foundry\IngestionRunsController;
use App\Http\Controllers\Foundry\IngestQualityController;
use App\Http\Controllers\Foundry\InvestigationsController;
use App\Http\Controllers\Foundry\LakehouseController;
use App\Http\Controllers\Foundry\OverviewController;
use App\Http\Controllers\Foundry\PortfolioController;
use App\Http\Controllers\Foundry\ProjectAnalyticsController;
use App\Http\Controllers\Foundry\ProjectsIndexController;
use App\Http\Controllers\Foundry\PublicGeoController;
use App\Http\Controllers\Foundry\RationaleController;
use App\Http\Controllers\Foundry\ReasoningController;
use App\Http\Controllers\Foundry\ReportController;
use App\Http\Controllers\Foundry\RetrievalInspectorController;
use App\Http\Controllers\Foundry\SavedMapViewsController;
use App\Http\Controllers\Foundry\SettingsController;
use App\Http\Controllers\Foundry\SourceGraphController;
use App\Http\Controllers\Foundry\SourcesController;
use App\Http\Controllers\Foundry\TargetsController;
use App\Http\Controllers\Foundry\Tier3Controller;
use App\Http\Controllers\Foundry\WorkspaceController;
use App\Http\Controllers\Internal\KestraSsoCheckController;
use App\Http\Controllers\Internal\MetricsController;
use App\Http\Controllers\InterpretationWorkspaceController;
use App\Http\Controllers\OAuthIngestController;
use App\Http\Controllers\OnboardingController;
use App\Http\Controllers\PublicGeoscience\TileProxyController as PublicGeoscienceTileProxy;
use App\Http\Controllers\PublicGeoscienceController;
use Illuminate\Foundation\Http\Middleware\VerifyCsrfToken;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Auth;
use Illuminate\Support\Facades\Route;
use Inertia\Inertia;
use Laravel\Sanctum\Http\Middleware\EnsureFrontendRequestsAreStateful;

// Module 10 Chunk 10.4 — Prometheus exposition. Unauthenticated by design;
// gated to private-IP callers in MetricsController::isAllowedScraper().
// Bypasses the auth + CSRF + Inertia middleware groups via withoutMiddleware.
Route::get('/metrics', MetricsController::class)
    ->withoutMiddleware([
        EnsureFrontendRequestsAreStateful::class,
        VerifyCsrfToken::class,
    ])
    ->name('metrics');

// Root redirects to /login. Authenticated users get bounced from /login to
// their dashboard by the Inertia auth flow, so this is a safe single entry
// point. The legacy Welcome page was removed 2026-05-21.
Route::get('/', function () {
    return redirect('/login');
});

Route::get('/login', function () {
    return Inertia::render('Login');
})->name('login');

Route::get('/forgot-password', function () {
    return Inertia::render('ForgotPassword');
})->name('password.request');

// Foundry sign-in surface — public, intentionally outside the auth group.
Route::get('/foundry/login', function () {
    return Inertia::render('Foundry/Login');
})->name('foundry.login');

// ── Authenticated routes (require Sanctum session or token) ─────────────
Route::middleware(['auth:sanctum'])->group(function () {
    // Foundry redesign (Wave 1+) — wired against real Wyoming Roll-Front Uranium
    // (Cameco Shirley Basin) data. Plan ~/.claude/plans/enumerated-tickling-bachman.md rev 7.
    Route::get('/dashboard', [PortfolioController::class, 'show'])
        ->name('dashboard');
    // Legacy /dashboard/legacy route removed 2026-05-18 along with
    // resources/js/Pages/Dashboard/* (the pre-Foundry UI). The Foundry
    // PortfolioController on /dashboard is the canonical entry point.

    Route::get('/projects', [ProjectsIndexController::class, 'show'])
        ->name('foundry.projects');

    // /projects/new MUST be declared before any /projects/{slug} route —
    // the wildcard's slug constraint is [a-z0-9\-]+, which "new" matches,
    // so without this priority Laravel routes to OverviewController and
    // 404s on the missing slug.
    //
    // /foundry/projects/new is the canonical new-project surface; this
    // path 301-redirects so any old bookmarks / external links still
    // land on the right page. The legacy resources/js/Pages/NewProject.tsx
    // was deleted on 2026-05-25 — Foundry/NewProject is the only render.
    Route::redirect('/projects/new', '/foundry/projects/new', 301)
        ->name('projects.new');

    Route::get('/projects/{slug}/targets', [TargetsController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')
        ->name('foundry.targets');

    Route::get('/projects/{slug}/targets/{targetId}/rationale', [RationaleController::class, 'show'])
        ->where(['slug' => '[a-z0-9\-]+', 'targetId' => '[a-zA-Z0-9\-]+'])
        ->name('foundry.rationale');

    Route::get('/projects/{slug}/compare', [HoleCompareController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')
        ->name('foundry.compare');

    Route::get('/projects/{slug}/imports/quality', [IngestQualityController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')
        ->name('foundry.ingest-quality');

    // Per-project ingestion-run progress (Phase A: derived from silver.reports
    // + bronze MinIO listing; Phase B will swap to silver.ingest_progress).
    // The .json variant powers the 5s poll from the IngestionRuns page and
    // the small Overview ingest card.
    Route::get('/projects/{slug}/ingestion-runs', [IngestionRunsController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')
        ->name('foundry.ingestion-runs');
    Route::get('/projects/{slug}/ingestion-runs.json', [IngestionRunsController::class, 'progress'])
        ->where('slug', '[a-z0-9\-]+')
        ->name('foundry.ingestion-runs.json');

    // Phase-22 §B/S/G build-out — Bronze + Silver + Gold inventory.
    Route::get('/projects/{slug}/lakehouse',
        [LakehouseController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')
        ->name('foundry.lakehouse');

    // Phase-22 §B/S/G build-out — Drillhole Detail (§5.12).
    Route::get('/projects/{slug}/holes/{collarId}/detail',
        [DrillholeDetailController::class, 'show'])
        ->where(['slug' => '[a-z0-9\-]+', 'collarId' => '[0-9a-fA-F-]{36}'])
        ->name('foundry.drillhole-detail');

    // CC-01 Item 1 Slice 4 — SRQ review surface for drill-data ingest.
    Route::get('/projects/{slug}/drill-review',
        [DrillReviewController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')
        ->name('foundry.drill-review');
    Route::post('/projects/{slug}/drill-review/{queueId}/decide',
        [DrillReviewController::class, 'decide'])
        ->where(['slug' => '[a-z0-9\-]+', 'queueId' => '[0-9a-fA-F-]{36}'])
        ->name('foundry.drill-review.decide');

    Route::get('/public-geoscience/tier3-unlock', [Tier3Controller::class, 'show'])
        ->name('foundry.tier3');
    Route::post('/public-geoscience/tier3-unlock', [Tier3Controller::class, 'request'])
        ->name('foundry.tier3.request');

    Route::get('/projects/{slug}/audit', [AuditLogController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')
        ->name('foundry.audit');

    Route::get('/projects/{slug}/analytics', [ProjectAnalyticsController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')
        ->name('foundry.project-analytics');

    Route::get('/retrieval/{traceId}', [RetrievalInspectorController::class, 'show'])
        ->where('traceId', '[a-zA-Z0-9\-]+')
        ->name('foundry.retrieval');

    Route::get('/projects/{slug}/whats-changed', [App\Http\Controllers\Foundry\WhatChangedController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')
        ->name('foundry.whats-changed');

    Route::get('/projects/{slug}/saved-views', [SavedMapViewsController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')
        ->name('foundry.saved-views');

    Route::get('/projects/{slug}/decisions', [DecisionsController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')
        ->name('foundry.decisions');
    Route::post('/projects/{slug}/decisions', [DecisionsController::class, 'store'])
        ->where('slug', '[a-z0-9\-]+')
        ->name('foundry.decisions.store');

    Route::get('/support-cockpit', [App\Http\Controllers\Foundry\SupportCockpitController::class, 'show'])
        ->name('foundry.support-cockpit');

    // /threads is deprecated — chat is project-scoped. Redirect to Portfolio
    // so the user picks a project and opens chat from inside it.
    Route::get('/threads', function () {
        return redirect()->route('dashboard');
    })->name('foundry.threads');

    // Project index — Overview dashboard. Clicking a Portfolio/Projects tile
    // lands here. The horizontal sub-bar + left rail are rendered by FoundryShell
    // because the URL starts with /projects/{slug}.
    Route::get('/projects/{slug}', [OverviewController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.project');

    // Chat lives inside projects — no standalone surface.
    Route::get('/projects/{slug}/chat', [ChatController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.project.chat');

    Route::get('/projects/{slug}/explorer', [ExplorerController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.explorer');
    Route::get('/projects/{slug}/workspace', [WorkspaceController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.workspace');
    Route::get('/projects/{slug}/holes/{hole}/payload', [WorkspaceController::class, 'holePayload'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.hole_payload');
    Route::get('/projects/{slug}/reasoning', [ReasoningController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.reasoning');
    Route::get('/projects/{slug}/hypothesis', [ReasoningController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.hypothesis');
    Route::get('/projects/{slug}/graph', [SourceGraphController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.graph');
    Route::get('/projects/{slug}/sources', [SourcesController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.sources');
    Route::get('/projects/{slug}/corpus', [CorpusController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.corpus');
    Route::get('/projects/{slug}/reports', [ReportController::class, 'index'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.reports');
    Route::get('/projects/{slug}/reports/{report_id}', [ReportController::class, 'view'])
        ->where(['slug' => '[a-z0-9\-]+', 'report_id' => '[0-9a-f-]{36}'])
        ->name('foundry.reports.view');
    // Figure manifest w/ presigned MinIO URLs (1-hour TTL). Lives behind
    // the Foundry auth shell so RLS scopes by workspace via Sanctum.
    Route::get('/projects/{slug}/reports/{report_id}/figures',
        [ReportController::class, 'figures'])
        ->where(['slug' => '[a-z0-9\-]+', 'report_id' => '[0-9a-f-]{36}'])
        ->name('foundry.reports.figures');
    // CC-01 Item 5 — Assessment report structured summary.
    Route::get('/projects/{slug}/reports/{report_id}/assessment-summary',
        [AssessmentSummaryController::class, 'show'])
        ->where(['slug' => '[a-z0-9\-]+', 'report_id' => '[0-9a-f-]{36}'])
        ->name('foundry.reports.assessment-summary');
    Route::post('/projects/{slug}/reports/{report_id}/assessment-summary/regenerate',
        [AssessmentSummaryController::class, 'regenerate'])
        ->where(['slug' => '[a-z0-9\-]+', 'report_id' => '[0-9a-f-]{36}'])
        ->name('foundry.reports.assessment-summary.regenerate');
    Route::post('/projects/{slug}/reports/{report_id}/completeness-audit/run',
        [AssessmentSummaryController::class, 'runCompletenessAudit'])
        ->where(['slug' => '[a-z0-9\-]+', 'report_id' => '[0-9a-f-]{36}'])
        ->name('foundry.reports.completeness-audit.run');
    Route::get('/projects/{slug}/investigations', [InvestigationsController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.investigations');

    Route::get('/inbox', [InboxController::class, 'show'])
        ->name('foundry.inbox');
    Route::get('/settings', [SettingsController::class, 'show'])
        ->name('foundry.settings');
    // The Foundry/PublicGeo page was a placeholder shell (no real MapLibre
    // instance). The functional UI lives at /public-geoscience; redirect
    // the legacy URL so old bookmarks land somewhere useful. The
    // PublicGeoController class still exists for reference but is no
    // longer wired to a route.
    Route::redirect('/foundry/public-geoscience', '/public-geoscience', 301)
        ->name('foundry.public-geoscience');
    Route::get('/foundry/imports/wizard', function () {
        return Inertia::render('Foundry/DataImportWizard');
    })->name('foundry.import-wizard');
    Route::get('/foundry/projects/new', function () {
        return Inertia::render('Foundry/NewProject');
    })->name('foundry.new-project');
    // Legacy /dashboard/projects/{slug} and /dashboard/projects/{slug}/analytics
    // routes removed 2026-05-18 along with resources/js/Pages/Dashboard/*.
    // The Foundry equivalents are /projects/{slug}/explorer and
    // /projects/{slug}/analytics — already wired above.

    // Convenience: when the user clicks "Analytics" in the top nav
    // without a project selected, send them to the Portfolio so they
    // can pick one. The DashboardLayout's project picker will then
    // deep-link to /dashboard/projects/{slug}/analytics on selection.
    Route::get('/analytics', function () {
        return redirect()->route('dashboard');
    })->name('analytics');

    // Standalone /chat is deprecated — chat is project-scoped. Redirect to
    // Portfolio so the user picks a project, then opens chat from inside it.
    Route::get('/chat', function () {
        return redirect()->route('dashboard');
    })->name('chat');

    Route::get('/explorer', function () {
        return Inertia::render('Explorer');
    })->name('explorer');

    // Phase 39 R-P11-B slice 1 — single-shot Search/Query surface.
    // Complements /chat (multi-turn). Skeleton ships first; SSE wiring
    // and citation reuse land in subsequent slices (Phases 40–43).
    Route::get('/search', function () {
        return Inertia::render('SearchQuery');
    })->name('search');

    // Public Geoscience — second read-only corpus (jurisdiction picker + map shell).
    // Uses a controller (rather than inline Inertia::render) because index() may
    // later pass server-side props (e.g. available jurisdictions) without a
    // client-side API round-trip. Currently it renders Inertia with no props.
    Route::get('/public-geoscience', [PublicGeoscienceController::class, 'index'])
        ->name('public-geoscience');

    // MVT tile proxy to Martin, whitelisted to the four public_geo
    // views. Keeping it on web.php (rather than api.php) lets the same route
    // serve SPA session-authenticated map tiles without a Bearer token
    // round-trip per request (Martin + MapLibre fire off hundreds of tile
    // GETs on pan/zoom).
    //
    // Rate limit: 600 requests/minute per authenticated user. MapLibre at
    // typical pan/zoom fires off 40-80 tile GETs per burst; 600/min gives
    // ~10 sustained bursts/min headroom without permitting a malicious
    // client to drown Martin. 429 bubbles back to MapLibre which degrades
    // gracefully (it retries stale cache + skips missing tiles).
    Route::middleware(['throttle:public-geoscience-tiles'])->group(function () {
        Route::get(
            '/tiles/public-geoscience/{source}/{z}/{x}/{y}.pbf',
            [PublicGeoscienceTileProxy::class, 'tile'],
        )
            ->where(['z' => '[0-9]+', 'x' => '[0-9]+', 'y' => '[0-9]+'])
            ->name('public-geoscience.tile');

        // Silver workspace-scoped MVT tile proxy.
        // Requires ?project_id={uuid} query param; enforces project access check.
        // Cache-Control: public, max-age=86400 (daily Silver refresh cadence).
        // ETag wiring: derived from silver.projects.data_version (§05d / PROXY-01 fix).
        Route::get(
            '/tiles/silver/{source}/{z}/{x}/{y}.pbf',
            [PublicGeoscienceTileProxy::class, 'silverTile'],
        )
            ->where(['z' => '[0-9]+', 'x' => '[0-9]+', 'y' => '[0-9]+'])
            ->name('silver.tile');
    });

    // /projects/new moved to the top of this group (line ~126) so it
    // beats the /projects/{slug} wildcard. Keeping a comment here for
    // grep-discoverability.

    // §19.3 Interpretation Workspace — Inertia page (notes / sections / zones).
    Route::get(
        '/projects/{projectId}/interpretation',
        [InterpretationWorkspaceController::class, 'index'],
    )->where('projectId', '[0-9a-fA-F-]{36}')
        ->name('projects.interpretation');

    // §17.3 Charts Gallery — Inertia page showcasing all 8 chart kinds.
    Route::get(
        '/charts/gallery',
        [ChartsGalleryController::class, 'gallery'],
    )->name('charts.gallery');

    // §16.1 — 6 missing customer dashboards (audit-flagged gap).
    Route::prefix('dashboards')->group(function () {
        Route::get('evidence-quality', [CustomerDashboardsController::class, 'evidenceQuality'])->name('dashboards.evidence-quality');
        Route::get('visual-readiness', [CustomerDashboardsController::class, 'visualReadiness'])->name('dashboards.visual-readiness');
        Route::get('publicgeo-overlay', [CustomerDashboardsController::class, 'publicGeoOverlay'])->name('dashboards.publicgeo-overlay');
        Route::get('target-recommendation', [CustomerDashboardsController::class, 'targetRecommendation'])->name('dashboards.target-recommendation');
        Route::get('reporting', [CustomerDashboardsController::class, 'reporting'])->name('dashboards.reporting');
        Route::get('llm-cost', [CustomerDashboardsController::class, 'llmCost'])->name('dashboards.llm-cost');
    });

    // §8.5 (step 3 deferred branch) — OAuth flows for cloud-source ingestion.
    // Functional scaffold; requires per-provider OAuth app registration
    // (see OAuthIngestController docstring + config/services.php).
    Route::get('/oauth/{provider}/authorize',
        [OAuthIngestController::class, 'start'])
        ->name('oauth.authorize')->where('provider', 'sharepoint|onedrive|googledrive');
    Route::get('/oauth/{provider}/callback',
        [OAuthIngestController::class, 'callback'])
        ->name('oauth.callback')->where('provider', 'sharepoint|onedrive|googledrive');
    Route::get('/oauth/connections',
        [OAuthIngestController::class, 'listConnections'])
        ->name('oauth.connections');

    // §8.5 Customer Onboarding Wizard — first-15-minutes activation funnel.
    Route::get('/onboarding', [OnboardingController::class, 'index'])
        ->name('onboarding');
    Route::post('/onboarding/step1', [OnboardingController::class, 'step1'])
        ->name('onboarding.step1');
    Route::post('/onboarding/step2', [OnboardingController::class, 'step2'])
        ->name('onboarding.step2');
    Route::post('/onboarding/step3', [OnboardingController::class, 'step3'])
        ->name('onboarding.step3');
    Route::post('/onboarding/complete', [OnboardingController::class, 'complete'])
        ->name('onboarding.complete');

    Route::post('/logout', function (Request $request) {
        Auth::guard('web')->logout();
        $request->session()->invalidate();
        $request->session()->regenerateToken();

        return redirect('/');
    })->name('logout');

    // ── Admin surfaces ──────────────────────────────────────────────────
    // The 'admin' Gate (users.is_admin = true) is enforced inside each
    // controller via $this->authorize('admin'); see AppServiceProvider::boot.
    // We deliberately do NOT use a Gate-driven middleware here because
    // Inertia + Sanctum returns 302→/login for unauthenticated reads, and
    // 403 for authenticated-but-not-admin — both of which the verifier
    // expects.

    // Phase 0 Step 3 — Workflow Run Dashboard.
    Route::get('/admin/workflow-runs', [WorkflowRunController::class, 'index'])
        ->name('admin.workflow-runs');

    // Phase 37 R-P21-CACHE-TELEMETRY-DASHBOARD — JSON endpoint + Phase 38
    // Inertia page. Both gated by the 'admin' Gate inside the controller
    // ($this->authorize('admin')) so guests redirect to /login, non-admin
    // users get 403, admins see the data.
    Route::get('/admin/cache-telemetry/skip-reasons.json', [CacheTelemetryController::class, 'skipReasons'])
        ->name('admin.cache-telemetry.skip-reasons');
    Route::get('/admin/cache-telemetry', [CacheTelemetryController::class, 'index'])
        ->name('admin.cache-telemetry');

    // Master-plan §3 Step 8 — Silver Review queue dashboard.
    // Doc-phase 58: index queue list. Doc-phase 60: show JSON + page-render
    // reverse-proxy for the React detail panel.
    Route::get('/admin/ingestion-review', [IngestionReviewController::class, 'index'])
        ->name('admin.ingestion-review');
    Route::get('/admin/ingestion-review/{review_item_id}.json', [IngestionReviewController::class, 'show'])
        ->where('review_item_id', '[0-9a-fA-F-]{36}')
        ->name('admin.ingestion-review.show');
    Route::get('/admin/ingestion-review/{review_item_id}/page/{page}.png', [IngestionReviewController::class, 'pageRender'])
        ->where('review_item_id', '[0-9a-fA-F-]{36}')
        ->where('page', '[0-9]+')
        ->name('admin.ingestion-review.page-render');
    // Doc-phase 61 — disposition update (PATCH). Resolved-is-terminal
    // enforced at the application layer; resolved_at + assigned_to
    // auto-populated when status transitions to a resolved_* value.
    Route::patch('/admin/ingestion-review/{review_item_id}', [IngestionReviewController::class, 'update'])
        ->where('review_item_id', '[0-9a-fA-F-]{36}')
        ->name('admin.ingestion-review.update');

    // Phase 4 Step 6 — Phase 1 Shadow comparison dashboard retired. The
    // silver.shadow_runs table was archived to S3 and dropped at the end
    // of the 30-day post-cutover window. Old route names removed; deep
    // links 404 cleanly.

    // Phase 1 Step 7 — Hatchet Worker Dashboard.
    Route::get('/admin/hatchet-workers', [HatchetWorkersController::class, 'index'])
        ->name('admin.hatchet-workers');

    // Doc-phase 183 — Cluster Ingest Dashboard. Phase A/B/C/D state
    // for large-archive ingestion (bronze.ingest_runs + manifest +
    // silver collars / passages / embeddings).
    Route::get('/admin/cluster-ingest', [ClusterIngestController::class, 'index'])
        ->name('admin.cluster-ingest');

    // Master-plan §10.7 (doc-phase 128) — Eval Dashboard.
    // Surfaces golden-question population, ontology progress, recent
    // eval runs. Reads eval.golden_questions + eval.run_summaries +
    // silver.geological_ontology_terms.
    Route::get('/admin/eval-dashboard', [EvalDashboardController::class, 'index'])
        ->name('admin.eval-dashboard');

    // Master-plan §10-v2 (doc-phase 179) — Golden Question Authoring UI.
    // Admin CRUD over eval.golden_questions, proxied through FastAPI.
    Route::get('/admin/eval/questions',
        [EvalQuestionsController::class, 'index'])
        ->name('admin.eval.questions.index');
    Route::get('/admin/eval/questions/new',
        [EvalQuestionsController::class, 'create'])
        ->name('admin.eval.questions.create');
    Route::get('/admin/eval/questions/{id}',
        [EvalQuestionsController::class, 'show'])
        ->name('admin.eval.questions.show')
        ->whereUuid('id');
    Route::post('/admin/eval/questions',
        [EvalQuestionsController::class, 'store'])
        ->name('admin.eval.questions.store');
    Route::put('/admin/eval/questions/{id}',
        [EvalQuestionsController::class, 'update'])
        ->name('admin.eval.questions.update')
        ->whereUuid('id');
    Route::post('/admin/eval/questions/{id}/transition',
        [EvalQuestionsController::class, 'transition'])
        ->name('admin.eval.questions.transition')
        ->whereUuid('id');
    Route::post('/admin/eval/questions/{id}/dry-run',
        [EvalQuestionsController::class, 'dryRun'])
        ->name('admin.eval.questions.dry-run')
        ->whereUuid('id');

    // Master-plan §10-v2 (doc-phase 179) — Eval Compare (trend + diff + drill).
    // Side-by-side comparison of any two eval runs with the §10.6
    // promotion-gate enforcer wired as the verdict action.
    Route::get('/admin/eval/compare',
        [EvalCompareController::class, 'index'])
        ->name('admin.eval.compare');
    Route::get('/admin/eval/compare/runs.json',
        [EvalCompareController::class, 'runsJson'])
        ->name('admin.eval.compare.runs');
    Route::get('/admin/eval/compare/runs/{id}.json',
        [EvalCompareController::class, 'perSetJson'])
        ->name('admin.eval.compare.per-set')
        ->whereUuid('id');
    Route::post('/admin/eval/compare/assess',
        [EvalCompareController::class, 'assess'])
        ->name('admin.eval.compare.assess');

    // Master-plan §9.12 (doc-phase 129) — Decision History.
    // Cross-workspace view of silver.decision_records + the audit_ledger
    // entries they anchor. Filterable by decision_type + workspace_id.
    Route::get('/admin/decision-history', [DecisionHistoryController::class, 'index'])
        ->name('admin.decision-history');

    // Doc-phase 158 — manual §21.3 decision-entry page.
    // Admin authentically files a decision of any of the 8 types when
    // the parent flow lacks a human-facing UI (e.g. crs_decision in
    // OCR triage, schema_mapping during data import).
    Route::get('/admin/decisions/new', [DecisionHistoryController::class, 'create'])
        ->name('admin.decisions.new');
    Route::post('/admin/decisions', [DecisionHistoryController::class, 'store'])
        ->name('admin.decisions.store');

    // Master-plan §10.11 / §25 (doc-phase 130) — Customer Support Cockpit.
    // Read-only view of ops.support_tickets + support_access audit anchors +
    // support_replay_runs. Filterable by status + severity + category.
    Route::get('/admin/support-cockpit', [SupportCockpitController::class, 'index'])
        ->name('admin.support-cockpit');
    // Phase G.5 follow-up — operator-triggered phase10 support agents.
    Route::post('/admin/support-cockpit/agents/{agent}', [SupportCockpitController::class, 'runAgent'])
        ->where('agent', '[a-z-]+')
        ->name('admin.support-cockpit.run-agent');

    // Master-plan §9.10 (doc-phase 131) — Hypothesis Workspace.
    // Cross-workspace view of silver.hypotheses + hypothesis_evidence_links.
    // Filterable by review_status + workspace_id.
    Route::get('/admin/hypothesis-workspace', [HypothesisWorkspaceController::class, 'index'])
        ->name('admin.hypothesis-workspace');

    // Phase H4 UI — §8 Target Recommendation Cockpit (R5 QP sign-off).
    Route::get('/admin/target-recommendation/runs',
        [TargetRecommendationCockpitController::class, 'index'])
        ->name('admin.target-recommendation.runs');
    Route::get('/admin/target-recommendation/runs/{run_id}',
        [TargetRecommendationCockpitController::class, 'show'])
        ->where('run_id', '[0-9a-f-]{36}')
        ->name('admin.target-recommendation.show');
    Route::post('/admin/target-recommendation/runs/{run_id}/signoff',
        [TargetRecommendationCockpitController::class, 'signoff'])
        ->where('run_id', '[0-9a-f-]{36}')
        ->name('admin.target-recommendation.signoff');
    Route::get('/admin/target-recommendation/runs/{run_id}/geojson',
        [TargetRecommendationCockpitController::class, 'geojson'])
        ->where('run_id', '[0-9a-f-]{36}')
        ->name('admin.target-recommendation.geojson');

    // Phase H4 UI — §7 Report Builder.
    Route::get('/admin/reports', [ReportBuilderController::class, 'index'])
        ->name('admin.reports.index');
    Route::post('/admin/reports/build', [ReportBuilderController::class, 'build'])
        ->name('admin.reports.build');
    Route::get('/admin/reports/{build_id}', [ReportBuilderController::class, 'show'])
        ->where('build_id', '[0-9a-f-]{36}')
        ->name('admin.reports.show');
    Route::put('/admin/reports/{build_id}/sections/{section_id}',
        [ReportBuilderController::class, 'saveSection'])
        ->where('build_id', '[0-9a-f-]{36}')
        ->name('admin.reports.save-section');
    Route::get('/admin/reports/{build_id}/sections/{section_id}/history',
        [ReportBuilderController::class, 'sectionHistory'])
        ->where('build_id', '[0-9a-f-]{36}')
        ->name('admin.reports.section-history');

    // Phase H4 UI — §12 ML training runs.
    Route::get('/admin/ml/training-runs', [MlTrainingRunsController::class, 'index'])
        ->name('admin.ml.training-runs');
    Route::post('/admin/ml/train-target-model',
        [MlTrainingRunsController::class, 'trainTargetModel'])
        ->name('admin.ml.train-target-model');
    Route::post('/admin/ml/train-source-trust',
        [MlTrainingRunsController::class, 'trainSourceTrust'])
        ->name('admin.ml.train-source-trust');

    // Phase H4 UI — §16 Grafana dashboards index + cross-links.
    Route::get('/admin/dashboards', [AdminDashboardsController::class, 'index'])
        ->name('admin.dashboards');

    // Phase H4 UI — §12.8 citation feedback (👍/👎 in ChatMessage).
    Route::post('/api/v1/citations/feedback',
        [CitationFeedbackController::class, 'submit'])
        ->name('citations.feedback');

    // Phase H4 UI Tier 2/3/4 — Conflicts review queue (§7.4)
    Route::get('/admin/conflicts', [ConflictsController::class, 'index'])
        ->name('admin.conflicts');
    Route::post('/admin/conflicts/run', [ConflictsController::class, 'run'])
        ->name('admin.conflicts.run');

    // Audit findings (combined: tenant isolation + cold-tier archive + boundary)
    Route::get('/admin/audit', [AuditFindingsController::class, 'index'])
        ->name('admin.audit');
    Route::post('/admin/audit/cold-tier-archive',
        [AuditFindingsController::class, 'triggerArchive'])
        ->name('admin.audit.cold-tier-archive');

    // What-Changed digest viewer (§9.9)
    Route::get('/admin/what-changed', [WhatChangedController::class, 'index'])
        ->name('admin.what-changed');

    // Tier 1 misc admin pages (§21.5 source-trust, §29 export-gate, §11.9 k6)
    Route::get('/admin/source-trust', [AdminMiscController::class, 'sourceTrust'])
        ->name('admin.source-trust');
    Route::get('/admin/export-gate', [AdminMiscController::class, 'exportGate'])
        ->name('admin.export-gate');
    Route::get('/admin/load-test', [AdminMiscController::class, 'loadTest'])
        ->name('admin.load-test');

    // Report export trigger (POST → §15 generate_report workflow).
    Route::post('/admin/reports/export', [ReportBuilderController::class, 'export'])
        ->name('admin.reports.export');

    // Tier 2/3/4 admin pages
    Route::get('/admin/recommendations', [Tier234Controller::class, 'recommendations'])->name('admin.recommendations');
    Route::post('/admin/recommendations/nbd', [Tier234Controller::class, 'runNbd'])->name('admin.recommendations.nbd');
    Route::post('/admin/recommendations/analogue', [Tier234Controller::class, 'runAnalogue'])->name('admin.recommendations.analogue');

    Route::get('/admin/qp-credentials', [Tier234Controller::class, 'qpCredentials'])->name('admin.qp-credentials');
    Route::post('/admin/qp-credentials', [Tier234Controller::class, 'createQp'])->name('admin.qp-credentials.create');
    Route::post('/admin/qp-credentials/{qp_credential_id}/verify', [Tier234Controller::class, 'verifyQp'])
        ->where('qp_credential_id', '[A-Za-z0-9_-]+')
        ->name('admin.qp-credentials.verify');

    Route::get('/admin/workspace-members', [Tier234Controller::class, 'workspaceMembers'])->name('admin.workspace-members');

    Route::get('/admin/workspace-settings/{workspace_id}', [Tier234Controller::class, 'workspaceSettings'])
        ->where('workspace_id', '[0-9a-f-]{36}')
        ->name('admin.workspace-settings.show');
    Route::post('/admin/workspace-settings/{workspace_id}', [Tier234Controller::class, 'saveWorkspaceSettings'])
        ->where('workspace_id', '[0-9a-f-]{36}')
        ->name('admin.workspace-settings.save');

    // Activepieces channel admin routes removed 2026-05-17 (Phase 0 cleanup).
    // Service sunset at Phase 3 Step 7; Kestra is the integration boundary.

    Route::get('/admin/audit-explorer', [Tier234Controller::class, 'auditExplorer'])->name('admin.audit-explorer');
    Route::get('/admin/audit-explorer/verify-chain',
        [Tier234Controller::class, 'verifyAuditChain'])
        ->name('admin.audit-explorer.verify-chain');

    // Phase H4 composite health surface — single page that lights up
    // every dependency the Phase H4 admin pages need.
    Route::get('/admin/phase-h4-health', [Tier234Controller::class, 'phaseH4Health'])
        ->name('admin.phase-h4-health');

    // §11.1 + §11.10 — backups + cold-tier dashboard.
    Route::get('/admin/backups', [Tier234Controller::class, 'backupsDashboard'])
        ->name('admin.backups');

    Route::get('/admin/saved-maps', [Tier234Controller::class, 'savedMaps'])->name('admin.saved-maps');

    // Phase H4 — audit-anchored alerts inbox (cost burn, vllm security, etc.).
    Route::get('/admin/alerts-inbox', [Tier234Controller::class, 'alertsInbox'])
        ->name('admin.alerts-inbox');
    Route::post('/admin/alerts-inbox/acknowledge',
        [Tier234Controller::class, 'acknowledgeAlert'])
        ->name('admin.alerts-inbox.ack');

    // Integrations dashboard (re-purposed for Kestra post-Activepieces sunset).
    Route::get('/admin/integrations', [IntegrationsController::class, 'index'])
        ->name('admin.integrations');
    Route::patch('/admin/integrations/flags/{flag_name}', [IntegrationsController::class, 'toggleFlag'])
        ->where('flag_name', 'flows\.[a-z_]+\.enabled')
        ->name('admin.integrations.toggle-flag');

    // Phase 4 Step 2 — Sanctum-fronted reverse proxy to Kestra UI/API.
    // `where('path', '.*')` lets it capture sub-paths + the empty root.
    Route::any('/admin/integrations/kestra/{path?}', [KestraSsoController::class, 'forward'])
        ->where('path', '.*')
        ->name('admin.integrations.kestra-sso');

    // Phase 6 Step 2 (R-P4-2) — forward_auth target for the Caddy edge.
    // Caddy subrequests this to validate the inbound session/Sanctum
    // token before proxying to Kestra; on 204, Caddy copies the
    // X-Kestra-Auth response header onto the upstream request so Kestra
    // sees a basic-auth-credentialed call.
    Route::get(
        '/internal/sanctum/check',
        [KestraSsoCheckController::class, 'check'],
    )->name('internal.sanctum.check');

    // Phase 4 Step 5 — per-sender HMAC registry enable/disable toggle.
    Route::patch('/admin/integrations/senders/{id}/{action}', [IntegrationsController::class, 'toggleSender'])
        ->where('id', '[0-9a-fA-F-]{36}')
        ->where('action', '(disable|enable)')
        ->name('admin.integrations.sender-toggle');

    // Phase 9 Step 2 (R-P8-1) — rotate-with-overlap for per-flow JWT keys.
    Route::post('/admin/integrations/jwt-keys/rotate', [IntegrationsController::class, 'rotateFlowKey'])
        ->name('admin.integrations.jwt-keys.rotate');

    // Phase 10 Step 3 — register a new external_notification sender.
    Route::post('/admin/integrations/senders', [IntegrationsController::class, 'registerSender'])
        ->name('admin.integrations.senders.register');

    // Phase 12 Step 4 (R-P10-1) — rotate a sender's HMAC.
    Route::post('/admin/integrations/senders/{id}/rotate-hmac', [IntegrationsController::class, 'rotateSenderHmac'])
        ->where('id', '[0-9a-fA-F-]{36}')
        ->name('admin.integrations.senders.rotate-hmac');

    // Phase 0 Step 5.2 — Agent Config (timeouts / prompts / pins / workspaces).
    Route::prefix('admin/agent-config')->name('admin.agent-config.')->group(function () {
        Route::get('/timeouts', [AdminAgentConfigTimeouts::class, 'index'])->name('timeouts');
        Route::patch('/timeouts/{agent_name}', [AdminAgentConfigTimeouts::class, 'update'])
            ->where('agent_name', '[A-Za-z0-9 _\-/.]+')
            ->name('timeouts.update');

        Route::get('/prompts', [AdminAgentConfigPrompts::class, 'index'])->name('prompts');
        Route::patch('/prompts/{id}/promote', [AdminAgentConfigPrompts::class, 'promote'])
            ->where('id', '[0-9a-fA-F-]{36}')
            ->name('prompts.promote');

        Route::get('/pins', [AdminAgentConfigPins::class, 'index'])->name('pins');
        Route::patch('/pins/{agent_name}', [AdminAgentConfigPins::class, 'update'])
            ->where('agent_name', '[A-Za-z0-9 _\-/.]+')
            ->name('pins.update');

        Route::get('/workspaces', [AdminAgentConfigWorkspaces::class, 'index'])->name('workspaces');
        Route::patch('/workspaces/{id}', [AdminAgentConfigWorkspaces::class, 'update'])
            ->where('id', '[0-9a-fA-F-]{36}')
            ->name('workspaces.update');
    });
});
