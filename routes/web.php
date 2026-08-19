<?php

declare(strict_types=1);

use App\Http\Controllers\Admin\IntegrationsController;
use App\Http\Controllers\CitationFeedbackController;
use App\Http\Controllers\Foundry\ChatController;
use App\Http\Controllers\Foundry\DrillholeDetailController;
use App\Http\Controllers\Foundry\HoleCompareController;
use App\Http\Controllers\Foundry\IngestionRunsController;
use App\Http\Controllers\Foundry\MapController;
use App\Http\Controllers\Foundry\OverviewController;
use App\Http\Controllers\Foundry\ProjectsIndexController;
use App\Http\Controllers\Foundry\PublicGeoscienceController;
use App\Http\Controllers\Foundry\ReportController;
use App\Http\Controllers\Foundry\SourcesController;
use App\Http\Controllers\Foundry\WorkspaceController;
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

Route::get('/reset-password/{token}', function (Request $request, string $token) {
    return Inertia::render('ResetPassword', [
        'token' => $token,
        'email' => $request->string('email')->toString(),
    ]);
})->name('password.reset');

// ── Authenticated routes (require Sanctum session or token) ─────────────
Route::middleware(['auth:sanctum'])->group(function () {
    Route::get('/projects', [ProjectsIndexController::class, 'show'])
        ->name('foundry.projects');

    // Not project-scoped — public_geo data isn't tenant data. Linked from
    // the top ORG nav bar. See PublicGeoscienceController docblock.
    Route::get('/public-geoscience', [PublicGeoscienceController::class, 'show'])
        ->name('foundry.public-geoscience');

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

    // Merged 2026-08-18 into the reports surface — the quality page was the
    // same silver.reports list with two extra per-document columns, so it now
    // lives as the quality strip + per-document status + reader Quality tab on
    // /reports. Kept as a named redirect (not deleted) so existing links,
    // bookmarks and any route('foundry.ingest-quality') callers keep working.
    //
    // 302 rather than 301 on purpose: a permanent redirect is cached by
    // browsers indefinitely and would be near-impossible to walk back if the
    // quality view is ever split out again.
    Route::get('/projects/{slug}/imports/quality', function (string $slug) {
        return redirect()->route('foundry.reports', ['slug' => $slug], 302);
    })
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
    // Standalone Map surface. MapView.tsx was previously only reachable
    // inline inside a chat answer (via InlineViz.tsx) — this route lets a
    // user navigate to it directly. See MapController docblock for why
    // GeoJSON is fetched client-side by MapView itself instead of here.
    Route::get('/projects/{slug}/map', [MapController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.map');
    // Merged 2026-08-18 into /reports — see ReportController's docblock. The
    // "Reader" nav item was a second document list over the same two tables;
    // its cross-document passage sample and entity-link rollup now fill the
    // reports pane when no document is selected. Named redirect so existing
    // route('foundry.corpus') callers and bookmarks keep working.
    Route::get('/projects/{slug}/corpus', function (string $slug) {
        return redirect()->route('foundry.reports', ['slug' => $slug], 302);
    })
        ->where('slug', '[a-z0-9\-]+')->name('foundry.corpus');
    // Restored 2026-08-17 (reader-core trim reversal, see plan addendum).
    Route::get('/projects/{slug}/holes/{collarId}/detail', [DrillholeDetailController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.drillhole-detail');
    Route::get('/projects/{slug}/compare', [HoleCompareController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.compare');
    Route::get('/projects/{slug}/workspace', [WorkspaceController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.workspace');
    Route::get('/projects/{slug}/holes/{hole}/payload', [WorkspaceController::class, 'holePayload'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.hole_payload');
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

    // Phase 4 Step 2 / Phase 6 Step 2 — Sanctum-fronted reverse proxy to
    // Kestra UI/API, and the Caddy forward_auth target that gated it.
    // REMOVED 2026-07-28 (A7): Kestra was never deployed (KESTRA_URL unset
    // in every environment) and the compose kestra + caddy services are
    // gone. See database/raw/phase3/95-kestra-sunset.sql.

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
