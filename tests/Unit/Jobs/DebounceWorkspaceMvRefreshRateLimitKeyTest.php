<?php

declare(strict_types=1);

namespace Tests\Unit\Jobs;

use App\Jobs\DebounceWorkspaceMvRefresh;
use Illuminate\Http\Client\Request;
use Illuminate\Support\Facades\Event;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Redis;
use Tests\TestCase;

/**
 * The MV-refresh call carries no user JWT, so FastAPI's rate limiter keys it
 * on the X-Workspace-Id header. Without the header every workspace's refresh
 * lands in the one bucket keyed on the Horizon worker's IP.
 *
 * The post-refresh data_version read fails without a database and the job
 * treats that as "no version info", so no fixtures are needed.
 */
class DebounceWorkspaceMvRefreshRateLimitKeyTest extends TestCase
{
    public function test_mv_refresh_request_names_the_workspace_for_rate_limit_keying(): void
    {
        config([
            'services.fastapi.internal_url' => 'http://fastapi.test',
            'services.fastapi.service_key' => 'test-key',
        ]);
        Redis::shouldReceive('get')->andReturn(null);
        Event::fake();
        Http::fake([
            'fastapi.test/internal/v1/mv-refresh/run' => Http::response(
                ['results' => [['status' => 'completed']]],
                200,
            ),
            'fastapi.test/internal/v1/metrics/*' => Http::response([], 200),
        ]);

        $workspaceId = 'a0000000-0000-0000-0000-000000000001';
        (new DebounceWorkspaceMvRefresh(
            $workspaceId,
            '019d74a1-fba8-7165-9ae6-a5bf93eef97d',
            'run-1',
            0,
        ))->handle();

        Http::assertSent(fn (Request $request): bool => $request->url() === 'http://fastapi.test/internal/v1/mv-refresh/run'
            && $request->hasHeader('X-Workspace-Id', $workspaceId));
    }
}
