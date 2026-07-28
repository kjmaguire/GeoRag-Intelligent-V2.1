<?php

declare(strict_types=1);

use App\Http\Controllers\Admin\IntegrationsController;
use App\Http\Controllers\Admin\KestraSsoController;
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
use App\Http\Controllers\Foundry\RationaleController;
use App\Http\Controllers\Foundry\ReasoningController;
use App\Http\Controllers\Foundry\ReportController;
use App\Http\Controllers\Foundry\RetrievalInspectorController;
use App\Http\Controllers\Foundry\SavedMapViewsController;
use App\Http\Controllers\Foundry\SettingsController;
use App\Http\Controllers\Foundry\SourceGraphController;
use App\Http\Controllers\Foundry\SourcesController;
use App\Http\Controllers\Foundry\TargetsController;
use App\Http\Controllers\Foundry\WorkspaceController;
use App\Http\Controllers\Internal\KestraSsoCheckController;
use App\Http\Controllers\Internal\MetricsController;
use App\Http\Controllers\InterpretationWorkspaceController;
use App\Http\Controllers\OAuthIngestController;
use App\Http\Controllers\OnboardingController;
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

    // Phase H4 UI — §12.8 citation feedback (👍/👎 in ChatMessage).
    Route::post('/api/v1/citations/feedback',
        [CitationFeedbackController::class, 'submit'])
        ->name('citations.feedback');

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

});
