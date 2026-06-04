<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Tests\TestCase;

/**
 * Pin the audit item F invariant: 'uploads' and 'charts' rate limiters
 * key on workspace_id, not user_id, and the routes that should be
 * throttled actually carry the middleware.
 *
 * Threat model
 * ------------
 * Per-user keying would let one operator inside workspace W drain the
 * compute / queue budget that other members of W share. Hatchet workers,
 * Dagster sensors, and chart-render workers all run inside the workspace
 * boundary; the rate limit should match that resource boundary, not the
 * identity boundary.
 *
 * Strategy mirrors the other tenancy regression tests in this dir:
 * file-content assertions so the test runs without a live DB or a live
 * RateLimiter facade. CI exercises the actual throttle response in the
 * request-level feature tests.
 */
class WorkspaceRateLimitsTest extends TestCase
{
    private function appServiceProvider(): string
    {
        $path = base_path('app/Providers/AppServiceProvider.php');
        $this->assertFileExists($path);

        return (string) file_get_contents($path);
    }

    private function apiRoutes(): string
    {
        $path = base_path('routes/api.php');
        $this->assertFileExists($path);

        return (string) file_get_contents($path);
    }

    public function test_uploads_limiter_is_registered(): void
    {
        $contents = $this->appServiceProvider();

        $this->assertMatchesRegularExpression(
            "/RateLimiter::for\\(\\s*'uploads'/",
            $contents,
            "RateLimiter::for('uploads', ...) must be registered. Without "
            .'it, the throttle:uploads middleware on POST /projects/{p}/upload '
            .'silently no-ops (Laravel\'s default behaviour for unregistered '
            .'limiter names is to skip throttling), re-opening the unbounded '
            .'upload firehose audit item F was opened to close.',
        );
        // Sized: 200/hr (see AppServiceProvider docblock for rationale).
        $this->assertMatchesRegularExpression(
            '/Limit::perHour\(200\)->by\(\$workspaceKey/',
            $contents,
            'Uploads limiter must be Limit::perHour(200)->by(workspaceKey). '
            .'Per-minute keying would surprise operators doing legitimate '
            .'150-file drops; the limit is sized for an hour of normal '
            .'bursty use, not for steady-state.',
        );
    }

    public function test_charts_limiter_is_registered(): void
    {
        $contents = $this->appServiceProvider();

        $this->assertMatchesRegularExpression(
            "/RateLimiter::for\\(\\s*'charts'/",
            $contents,
            "RateLimiter::for('charts', ...) must be registered.",
        );
        $this->assertMatchesRegularExpression(
            '/Limit::perMinute\(60\)->by\(\$workspaceKey/',
            $contents,
            'Charts limiter must be Limit::perMinute(60)->by(workspaceKey). '
            .'Per-hour keying would let a re-render loop ship 3,600 requests '
            .'before throttling — minute-grain is the right resolution for '
            .'interactive bursts.',
        );
    }

    public function test_workspace_key_resolution_chain(): void
    {
        $contents = $this->appServiceProvider();

        // Resolution order: session('current_workspace_id') → user
        // ->defaultWorkspaceId() → user id → IP → sentinel. The chain
        // matters: anonymous traffic must still hit a real bucket so a
        // crawler can't infinitely escape by stripping cookies.
        $this->assertStringContainsString(
            "session()->get('current_workspace_id')",
            $contents,
            'Limiter key must first try session(current_workspace_id). '
            .'This session key is populated by HandleInertiaRequests (audit '
            .'item A) — drift between the share layer and the limiter '
            .'resolution would silently demote every request to per-user.',
        );
        $this->assertStringContainsString(
            'defaultWorkspaceId()',
            $contents,
            'Limiter key must fall back to User->defaultWorkspaceId() when '
            .'session has no current_workspace_id yet (first-request-of-session '
            .'case).',
        );
        // IP fallback proves anonymous traffic still buckets, just not
        // per-workspace. Without this clause an anonymous request would
        // share the sentinel bucket with every other anonymous request.
        $this->assertStringContainsString(
            "':ip:'",
            $contents,
            'Limiter key must fall back to IP for anonymous traffic.',
        );
    }

    public function test_upload_routes_carry_throttle_uploads(): void
    {
        $contents = $this->apiRoutes();

        // The two upload endpoints share the SAME bucket name so a user
        // doing both kinds of upload in one session doesn't get double
        // the budget.
        $this->assertMatchesRegularExpression(
            "/projects\\/\\{project\\}\\/upload.*?->middleware\\('throttle:uploads'\\)/s",
            $contents,
            'POST projects/{project}/upload must carry throttle:uploads. '
            .'Without it the edge limiter is missing and the only backstop '
            .'is the dispatch-side HatchetDispatchThrottle — which fires '
            .'AFTER the request is accepted (cost already paid).',
        );
        $this->assertMatchesRegularExpression(
            "/projects\\/\\{slug\\}\\/drill-uploads.*?->middleware\\('throttle:uploads'\\)/s",
            $contents,
            'POST projects/{slug}/drill-uploads must carry throttle:uploads. '
            .'Same bucket as the generic upload so the per-workspace cap '
            .'composes across both upload kinds.',
        );
    }

    public function test_charts_render_carries_throttle_charts(): void
    {
        $contents = $this->apiRoutes();

        $this->assertMatchesRegularExpression(
            "/charts\\/render.*?->middleware\\('throttle:charts'\\)/s",
            $contents,
            'POST charts/render must carry throttle:charts. The render '
            .'endpoint can fan out to Plotly/duckdb/postgres in 8 shapes; '
            .'a runaway frontend re-render loop without this throttle '
            .'would pin compute for the whole workspace.',
        );
    }

    public function test_artisan_is_naturally_exempt(): void
    {
        // Artisan exemption is structural: rate limiters are middleware,
        // and artisan commands never traverse the HTTP middleware stack.
        // This test pins the rationale comment so a future contributor
        // doesn't "helpfully" add a per-command rate limiter that would
        // throttle the very backfills that the docs tell operators to
        // run AROUND the UI limit.
        $contents = $this->appServiceProvider();

        $this->assertStringContainsString(
            'Artisan exemption is automatic',
            $contents,
            'AppServiceProvider must document that artisan is exempt from '
            .'the uploads/charts limiters — operators rely on the artisan '
            .'reingest path to run bulk work around the UI cap (cameco '
            .'recovery 2026-06-02 used this exact escape hatch).',
        );
    }
}
