<?php

declare(strict_types=1);

namespace Tests\Feature\Jobs;

use App\Events\Admin\AdminSurfaceUpdated;
use App\Events\Workspace\WorkspaceActivityBroadcast;
use App\Events\WorkspaceDataUpdated;
use App\Jobs\DebounceWorkspaceMvRefresh;
use Illuminate\Support\Facades\Event;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Redis;
use Illuminate\Support\Str;
use RuntimeException;
use Tests\TestCase;

/**
 * When a materialized view fails to refresh, open pages must still reload the
 * non-MV data (reports, quality, ...) and the job must retry the MV part.
 * Before, a `failed` view suppressed WorkspaceDataUpdated entirely and the
 * job returned success, so nothing reloaded and nothing retried.
 */
final class DebounceWorkspaceMvRefreshFailedViewTest extends TestCase
{
    private string $workspaceId;

    protected function setUp(): void
    {
        parent::setUp();

        $this->workspaceId = (string) Str::uuid();

        config([
            'services.fastapi.internal_url' => 'http://fastapi.test',
            'services.fastapi.service_key' => 'test-only-service-key-with-at-least-32-bytes',
        ]);

        Redis::shouldReceive('get')->andReturn(null);
        Event::fake([WorkspaceDataUpdated::class, WorkspaceActivityBroadcast::class, AdminSurfaceUpdated::class]);
    }

    private function job(): DebounceWorkspaceMvRefresh
    {
        return new DebounceWorkspaceMvRefresh($this->workspaceId, (string) Str::uuid(), (string) Str::uuid(), time(), 'tok');
    }

    public function test_a_failed_view_still_emits_the_non_mv_update_then_throws_for_retry(): void
    {
        Http::fake([
            'fastapi.test/internal/v1/mv-refresh/run' => Http::response([
                'results' => [['view_name' => 'silver.mv_collar_summary', 'status' => 'failed']],
            ]),
        ]);

        try {
            $this->job()->handle();
            $this->fail('the job must throw so the queue retries the MV refresh');
        } catch (RuntimeException $e) {
            $this->assertStringContainsString('failed to refresh', $e->getMessage());
        }

        Event::assertDispatched(WorkspaceDataUpdated::class, function (WorkspaceDataUpdated $e): bool {
            return in_array('reports', $e->affectedTypes, true)
                && in_array('quality', $e->affectedTypes, true)
                && ! in_array('collars', $e->affectedTypes, true)
                && ! in_array('assays', $e->affectedTypes, true);
        });
        Event::assertDispatched(WorkspaceActivityBroadcast::class);
        Event::assertNotDispatched(AdminSurfaceUpdated::class);
    }

    public function test_the_job_keeps_its_retry_configuration(): void
    {
        $job = $this->job();

        $this->assertGreaterThan(1, $job->tries);
        $this->assertGreaterThan(0, $job->backoff);
    }

    public function test_a_clean_refresh_emits_everything_and_does_not_throw(): void
    {
        Http::fake([
            'fastapi.test/internal/v1/mv-refresh/run' => Http::response([
                'results' => [['view_name' => 'silver.mv_collar_summary', 'status' => 'completed']],
            ]),
            'fastapi.test/internal/v1/metrics/*' => Http::response([], 200),
        ]);

        $this->job()->handle();

        Event::assertDispatched(WorkspaceDataUpdated::class, fn (WorkspaceDataUpdated $e): bool => in_array('collars', $e->affectedTypes, true));
        Event::assertDispatched(AdminSurfaceUpdated::class);
    }
}
