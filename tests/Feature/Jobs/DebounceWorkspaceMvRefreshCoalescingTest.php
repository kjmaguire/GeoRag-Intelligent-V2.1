<?php

declare(strict_types=1);

namespace Tests\Feature\Jobs;

use App\Events\Admin\AdminSurfaceUpdated;
use App\Events\Workspace\WorkspaceActivityBroadcast;
use App\Events\WorkspaceDataUpdated;
use App\Jobs\DebounceWorkspaceMvRefresh;
use Illuminate\Contracts\Queue\ShouldBeUnique;
use Illuminate\Http\Client\Request as HttpRequest;
use Illuminate\Support\Collection;
use Illuminate\Support\Facades\Event;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Queue;
use Illuminate\Support\Facades\Redis;
use Illuminate\Support\Str;
use RedisException;
use Tests\TestCase;

/**
 * LAR-3 (2026-09-29 audit): a burst of ingestion completions must produce
 * exactly ONE MV refresh — and one WorkspaceDataUpdated — after the quiet
 * period. Never zero.
 *
 * The old job combined ShouldBeUnique with a "newer stamp → bail" check. The
 * unique lock rejected completion B's dispatch inside A's 30 s delay, and A
 * then bailed on B's newer stamp: two completions within 30 s refreshed
 * nothing. These tests drive the real dispatch path (debounce()) with the
 * queue faked, travel through the delay, and run what was queued in order.
 *
 * Redis is replaced with an in-memory map: the stamp is the only thing the
 * job keeps there, and this suite has no Redis server.
 */
final class DebounceWorkspaceMvRefreshCoalescingTest extends TestCase
{
    /** @var array<string, string> */
    private array $redis = [];

    private bool $redisDown = false;

    private string $workspaceId;

    private string $projectId;

    protected function setUp(): void
    {
        parent::setUp();

        $this->workspaceId = (string) Str::uuid();
        $this->projectId = (string) Str::uuid();

        config([
            'services.fastapi.internal_url' => 'http://fastapi.test',
            'services.fastapi.service_key' => 'test-only-service-key-with-at-least-32-bytes',
        ]);

        Redis::shouldReceive('setex')->andReturnUsing(function (string $key, int $ttl, string $value): bool {
            $this->redis[$key] = $value;

            return true;
        });
        Redis::shouldReceive('get')->andReturnUsing(function (string $key): ?string {
            if ($this->redisDown) {
                throw new RedisException('Connection refused');
            }

            return $this->redis[$key] ?? null;
        });

        Http::fake([
            'fastapi.test/internal/v1/mv-refresh/run' => Http::response([
                'results' => [['view_name' => 'silver.mv_collar_summary', 'status' => 'completed']],
            ]),
            'fastapi.test/internal/v1/metrics/*' => Http::response([], 200),
        ]);

        Event::fake([WorkspaceDataUpdated::class, WorkspaceActivityBroadcast::class, AdminSurfaceUpdated::class]);
        Queue::fake();
        $this->freezeTime();
    }

    /**
     * Run every queued refresh job in dispatch order, each once its delay has
     * elapsed — what a Horizon worker would do.
     */
    private function runQueuedJobs(): void
    {
        /** @var Collection<int, DebounceWorkspaceMvRefresh> $jobs */
        $jobs = Queue::pushed(DebounceWorkspaceMvRefresh::class);

        foreach ($jobs as $job) {
            $this->travelTo($job->delay);
            $job->handle();
        }
    }

    private function refreshCalls(): int
    {
        return Http::recorded(fn (HttpRequest $r): bool => str_ends_with($r->url(), '/internal/v1/mv-refresh/run'))->count();
    }

    public function test_a_burst_of_completions_refreshes_exactly_once_after_the_last(): void
    {
        $runs = [(string) Str::uuid(), (string) Str::uuid(), (string) Str::uuid()];

        DebounceWorkspaceMvRefresh::debounce($this->workspaceId, $this->projectId, $runs[0]);
        $this->travel(10)->seconds();
        DebounceWorkspaceMvRefresh::debounce($this->workspaceId, $this->projectId, $runs[1]);
        $this->travel(10)->seconds();
        DebounceWorkspaceMvRefresh::debounce($this->workspaceId, $this->projectId, $runs[2]);
        $lastCompletionAt = now();

        // Every completion is queued: nothing is refused at dispatch time.
        Queue::assertPushed(DebounceWorkspaceMvRefresh::class, 3);

        $this->runQueuedJobs();

        $this->assertSame(1, $this->refreshCalls());
        Event::assertDispatchedTimes(WorkspaceDataUpdated::class, 1);
        Event::assertDispatched(
            WorkspaceDataUpdated::class,
            fn (WorkspaceDataUpdated $e): bool => $e->pipelineRunId === $runs[2],
        );

        // The one refresh ran after the quiet period that followed the LAST
        // completion, not the first.
        $this->assertTrue(now()->greaterThanOrEqualTo(
            $lastCompletionAt->copy()->addSeconds(DebounceWorkspaceMvRefresh::DEBOUNCE_SECONDS),
        ));
    }

    public function test_a_single_completion_refreshes_once(): void
    {
        DebounceWorkspaceMvRefresh::debounce($this->workspaceId, $this->projectId, (string) Str::uuid());

        $this->runQueuedJobs();

        $this->assertSame(1, $this->refreshCalls());
        Event::assertDispatchedTimes(WorkspaceDataUpdated::class, 1);
    }

    public function test_two_bursts_separated_by_a_quiet_period_refresh_once_each(): void
    {
        DebounceWorkspaceMvRefresh::debounce($this->workspaceId, $this->projectId, (string) Str::uuid());
        $this->runQueuedJobs();

        Queue::fake();
        $this->travel(5)->minutes();
        DebounceWorkspaceMvRefresh::debounce($this->workspaceId, $this->projectId, (string) Str::uuid());
        $this->travel(5)->seconds();
        DebounceWorkspaceMvRefresh::debounce($this->workspaceId, $this->projectId, (string) Str::uuid());
        $this->runQueuedJobs();

        $this->assertSame(2, $this->refreshCalls());
        Event::assertDispatchedTimes(WorkspaceDataUpdated::class, 2);
    }

    public function test_bursts_in_different_workspaces_do_not_coalesce_with_each_other(): void
    {
        DebounceWorkspaceMvRefresh::debounce($this->workspaceId, $this->projectId, (string) Str::uuid());
        DebounceWorkspaceMvRefresh::debounce((string) Str::uuid(), (string) Str::uuid(), (string) Str::uuid());

        $this->runQueuedJobs();

        $this->assertSame(2, $this->refreshCalls());
    }

    public function test_an_unreadable_stamp_still_refreshes(): void
    {
        DebounceWorkspaceMvRefresh::debounce($this->workspaceId, $this->projectId, (string) Str::uuid());

        // Redis goes down between dispatch and handle().
        $this->redisDown = true;

        $this->runQueuedJobs();

        $this->assertSame(1, $this->refreshCalls());
    }

    public function test_a_legacy_job_with_no_token_is_not_starved_by_its_own_timestamp_stamp(): void
    {
        $dispatchedAt = now()->getTimestamp();
        $this->redis[DebounceWorkspaceMvRefresh::stampKey($this->workspaceId)] = (string) $dispatchedAt;

        (new DebounceWorkspaceMvRefresh($this->workspaceId, $this->projectId, (string) Str::uuid(), $dispatchedAt))->handle();

        $this->assertSame(1, $this->refreshCalls());
    }

    public function test_the_job_is_no_longer_unique(): void
    {
        // The unique lock is what dropped the second completion. Pin that it
        // stays gone.
        $this->assertNotInstanceOf(
            ShouldBeUnique::class,
            new DebounceWorkspaceMvRefresh($this->workspaceId, $this->projectId, (string) Str::uuid(), time()),
        );
    }
}
