<?php

declare(strict_types=1);

namespace Tests\Unit\Jobs;

use App\Jobs\DebounceWorkspaceMvRefresh;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * LAR-7 (2026-09-29 audit): the FastAPI call must finish (or give up) before
 * the worker kills the job.
 *
 * The job waited 120 s on /internal/v1/mv-refresh/run while supervisor-1's
 * worker timeout is 60 s, so a slow refresh was SIGKILLed mid-request, sat in
 * `reserved` until retry_after, and was killed again on each retry. The order
 * that must hold:
 *
 *   connect + HTTP timeout  <  job $timeout  <=  supervisor-1 timeout  <  retry_after
 */
final class DebounceWorkspaceMvRefreshTimeoutTest extends TestCase
{
    public function test_http_timeout_fits_inside_the_worker_timeout(): void
    {
        $job = new DebounceWorkspaceMvRefresh(
            (string) Str::uuid(),
            (string) Str::uuid(),
            (string) Str::uuid(),
            time(),
        );

        $httpBudget = DebounceWorkspaceMvRefresh::HTTP_TIMEOUT_SECONDS
            + DebounceWorkspaceMvRefresh::HTTP_CONNECT_TIMEOUT_SECONDS;

        $this->assertLessThan($job->timeout, $httpBudget);
        $this->assertLessThanOrEqual((int) config('horizon.defaults.supervisor-1.timeout'), $job->timeout);
        $this->assertLessThan(
            (int) config('queue.connections.'.config('horizon.defaults.supervisor-1.connection').'.retry_after'),
            $job->timeout,
        );
    }

    public function test_it_runs_on_the_queue_supervisor_1_serves(): void
    {
        $job = new DebounceWorkspaceMvRefresh(
            (string) Str::uuid(),
            (string) Str::uuid(),
            (string) Str::uuid(),
            time(),
        );

        $this->assertContains($job->queue, (array) config('horizon.defaults.supervisor-1.queue'));
    }
}
