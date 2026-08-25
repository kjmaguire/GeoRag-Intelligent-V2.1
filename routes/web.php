<?php

declare(strict_types=1);

use App\Http\Controllers\Admin\IntegrationsController;
use App\Http\Controllers\CitationFeedbackController;
use App\Http\Controllers\Foundry\AttributeTablesController;
use App\Http\Controllers\Foundry\ChatController;
use App\Http\Controllers\Foundry\DrillholeDetailController;
use App\Http\Controllers\Foundry\IngestionRunsController;
use App\Http\Controllers\Foundry\OverviewController;
use App\Http\Controllers\Foundry\ProjectsIndexController;
use App\Http\Controllers\Foundry\PublicGeoscienceController;
use App\Http\Controllers\Foundry\RasterLayersController;
use App\Http\Controllers\Foundry\ReportController;
use App\Http\Controllers\Foundry\SourcesController;
use App\Http\Controllers\Foundry\WorkspaceController;
use App\Http\Controllers\Internal\MetricsController;
use App\Http\Controllers\OAuthIngestController;
use App\Http\Controllers\PublicGeoscience\TileProxyController as PublicGeoscienceTileProxy;
use Illuminate\Foundation\Http\Middleware\VerifyCsrfToken;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Auth;
use Illuminate\Support\Facades\Route;
use Inertia\Inertia;
use Laravel\Sanctum\Http\Middleware\EnsureFrontendRequestsAreStateful;

// Module 10 Chunk 10.4 — Prometheus exposition. No session (a scraper has
// none), but it does need the `service.key` shared secret: the previous gate
// compared $request->ip() against RFC-1918 ranges, and ip() reads the
// client-supplied X-Forwarded-For chain, so the whole metric set was readable
// by anyone willing to send one header. Bypasses the auth + CSRF + Inertia
// middleware groups via withoutMiddleware.
Route::get('/metrics', MetricsController::class)
    ->middleware('service.key')
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

    // Two tables that had writers and no readers. Data landed in both and
    // nothing in the product ever mentioned it again — a real delivery wrote
    // 229 attribute rows and 4 raster rows that a geologist could not see.
    Route::get('/projects/{slug}/attribute-tables', [AttributeTablesController::class, 'index'])
        ->where('slug', '[a-z0-9\-]+')
        ->name('foundry.attribute_tables');

    Route::get('/projects/{slug}/rasters', [RasterLayersController::class, 'index'])
        ->where('slug', '[a-z0-9\-]+')
        ->name('foundry.rasters');

    // MVT tile proxy to Martin. On web.php rather than api.php so the same
    // route serves SPA session-authenticated map tiles without a Bearer token
    // round-trip per request — MapLibre fires hundreds of tile GETs on a
    // single pan/zoom.
    //
    // Restored 2026-08-25. Removed in 0eada56c ("remove demo-external
    // services") along with Martin itself; the 18 PostGIS tile functions it
    // proxies were never removed and are live on the Azure server.
    Route::middleware(['throttle:public-geoscience-tiles'])->group(function () {
        Route::get(
            '/tiles/public-geoscience/{source}/{z}/{x}/{y}.pbf',
            [PublicGeoscienceTileProxy::class, 'tile'],
        )
            ->where(['z' => '[0-9]+', 'x' => '[0-9]+', 'y' => '[0-9]+'])
            ->name('public-geoscience.tile');

        // Silver workspace-scoped tiles. Requires ?project_id={uuid} and
        // enforces the project-access check; ETag derives from
        // silver.projects.data_version.
        Route::get(
            '/tiles/silver/{source}/{z}/{x}/{y}.pbf',
            [PublicGeoscienceTileProxy::class, 'silverTile'],
        )
            ->where(['z' => '[0-9]+', 'x' => '[0-9]+', 'y' => '[0-9]+'])
            ->name('silver.tile');
    });

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
    // Re-run a refused tabular ingest with a column mapping the user
    // confirmed. The bytes are already in bronze, so nothing is re-uploaded.
    Route::post('/projects/{slug}/ingestion-runs/remap', [IngestionRunsController::class, 'remap'])
        ->where('slug', '[a-z0-9\-]+')
        ->name('foundry.ingestion-runs.remap');

    // Project index. The horizontal sub-bar + left rail are rendered by FoundryShell
    // because the URL starts with /projects/{slug}.
    Route::get('/projects/{slug}', [OverviewController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.project');

    // Chat lives inside projects — no standalone surface.
    Route::get('/projects/{slug}/chat', [ChatController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.project.chat');

    Route::get('/projects/{slug}/sources', [SourcesController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.sources');
    // Merged 2026-08-19 into /workspace — see WorkspaceController's docblock.
    // The standalone Map page rendered MapView with a self-fetch against
    // /api/v1/projects/{project}/collars; the workspace's MAP mode renders
    // WorkspaceMap over the same collars with uncertainty rings, ore-band
    // styling, basemap + terrain switching, layer toggles and the
    // click-two-pins compare queue. Keeping both meant two map surfaces to
    // maintain and a nav that implied they showed different things.
    // Named redirect so route('foundry.map') callers and bookmarks keep
    // working; MAP is the workspace's default mode, so no ?mode= is needed.
    Route::get('/projects/{slug}/map', function (string $slug) {
        return redirect()->route('foundry.workspace', ['slug' => $slug], 302);
    })
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
    // Merged 2026-08-19 into /workspace's COMPARE mode. The standalone page
    // was a strictly weaker duplicate: it hydrated collar metadata plus a
    // plain-text lithology list and hardcoded grade_avg / grade_top /
    // rock_summary / intercepts to null, while the workspace's
    // holePayload() + CompareHolesPanel render real log curves, colour-coded
    // lithology bands, ore-band counts and mean grade. ?mode=compare so the
    // redirect lands on the comparison rather than on MAP — see
    // Workspace.tsx's initialMode(). The old ?left=/?right= query pair is
    // NOT carried across: selection is now client-side in the panel's
    // dropdowns, so there is no server-side prop for them to populate.
    Route::get('/projects/{slug}/compare', function (string $slug) {
        return redirect()->to(route('foundry.workspace', ['slug' => $slug]).'?mode=compare', 302);
    })
        ->where('slug', '[a-z0-9\-]+')->name('foundry.compare');
    Route::get('/projects/{slug}/workspace', [WorkspaceController::class, 'show'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.workspace');
    Route::get('/projects/{slug}/holes/{hole}/payload', [WorkspaceController::class, 'holePayload'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.hole_payload');
    Route::get('/projects/{slug}/reports', [ReportController::class, 'index'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.reports');
    // Short-lived presigned URL for the bronze object a report was parsed
    // from — powers the reader's ORIGINAL tab, where the extracted text sits
    // next to the page it came from. Membership-gated in the controller;
    // 404s rather than 403s across project boundaries.
    Route::get('/projects/{slug}/reports/{report_id}/source', [ReportController::class, 'source'])
        ->where('slug', '[a-z0-9\-]+')->name('foundry.report.source');
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
