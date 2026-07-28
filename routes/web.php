<?php

declare(strict_types=1);

use App\Http\Controllers\Admin\IntegrationsController;
use App\Http\Controllers\Admin\KestraSsoController;
use App\Http\Controllers\CitationFeedbackController;
use App\Http\Controllers\Foundry\ChatController;
use App\Http\Controllers\Foundry\CorpusController;
use App\Http\Controllers\Foundry\IngestionRunsController;
use App\Http\Controllers\Foundry\IngestQualityController;
use App\Http\Controllers\Foundry\OverviewController;
use App\Http\Controllers\Foundry\ProjectsIndexController;
use App\Http\Controllers\Foundry\ReportController;
use App\Http\Controllers\Foundry\SourcesController;
use App\Http\Controllers\Internal\KestraSsoCheckController;
use App\Http\Controllers\Internal\MetricsController;
use App\Http\Controllers\OAuthIngestController;
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

// Root redirects to the reader-core login.
Route::get('/', function () {
    return redirect('/login');
});

Route::get('/login', function () {
    return Inertia::render('Login');
})->name('login');

Route::get('/forgot-password', function () {
    return Inertia::render('ForgotPassword');
})->name('password.request');

// ── Authenticated routes (require Sanctum session or token) ─────────────
Route::middleware(['auth:sanctum'])->group(function () {
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

    // Project index. The horizontal sub-bar + left rail are rendered by FoundryShell
    // because the URL starts with /projects/{slug}.
    Route::get('/projects/{slug}', [OverviewController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.project');

    // Chat lives inside projects — no standalone surface.
    Route::get('/projects/{slug}/chat', [ChatController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.project.chat');

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
    Route::get('/foundry/imports/wizard', function () {
        return Inertia::render('Foundry/DataImportWizard');
    })->name('foundry.import-wizard');
    Route::get('/foundry/projects/new', function () {
        return Inertia::render('Foundry/NewProject');
    })->name('foundry.new-project');
    // /projects/new moved to the top of this group (line ~126) so it
    // beats the /projects/{slug} wildcard. Keeping a comment here for
    // grep-discoverability.

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
