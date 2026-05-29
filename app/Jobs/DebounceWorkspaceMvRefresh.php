<?php

declare(strict_types=1);

namespace App\Jobs;

use App\Events\Admin\AdminSurfaceUpdated;
use App\Events\Workspace\WorkspaceActivityBroadcast;
use App\Events\WorkspaceDataUpdated;
use App\Http\Controllers\Internal\IngestionProgressBroadcastController;
use Illuminate\Contracts\Queue\ShouldBeUnique;
use Illuminate\Contracts\Queue\ShouldQueue;
use Illuminate\Foundation\Queue\Queueable;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Facades\Redis;
use RuntimeException;

/**
 * Phase 2 of the reliability spec — debounced per-workspace MV refresh.
 *
 * Dispatched from {@see IngestionProgressBroadcastController}
 * whenever a completed-status ingestion event lands. The job runs after
 * a 30-second delay so a burst of completions for the same workspace
 * coalesces into a single REFRESH MATERIALIZED VIEW call — the Phase 1
 * Ontario Gold re-ingest fired 9 completions inside a minute, and we
 * don't want to pay 9× REFRESH cost when one will do.
 *
 * Coalescing works through two layers:
 *
 *   1. **ShouldBeUnique** — Laravel's built-in deduper. As long as a job
 *      with the same `uniqueId()` is queued OR running, additional
 *      dispatches are rejected. TTL caps how long the dedup key holds
 *      (so a stuck/dead job doesn't permanently shadow refreshes).
 *
 *   2. **Per-workspace Redis "last-dispatch" stamp** — recorded by the
 *      controller before dispatching. The job's first action at handle()
 *      is to check whether a NEWER dispatch arrived during the delay
 *      window. If yes, it bails (a fresher job is queued and will do
 *      the work). This is the "reset the timer on subsequent dispatch"
 *      semantic the spec calls for.
 *
 * When the job decides to run, it POSTs to FastAPI's
 * /internal/v1/mv-refresh/run endpoint, which performs the actual
 * REFRESH under per-view advisory locks + logs to gold.mv_refresh_log.
 *
 * On successful refresh, the job dispatches a
 * {@see WorkspaceDataUpdated} event so the frontend pages
 * (Overview/Lakehouse/Drillhole/Map) know to re-fetch their data.
 */
class DebounceWorkspaceMvRefresh implements ShouldBeUnique, ShouldQueue
{
    use Queueable;

    /** Refresh debounce window — must match the dispatch delay. */
    public const DEBOUNCE_SECONDS = 30;

    public int $tries = 3;

    public int $backoff = 30;

    public function __construct(
        public readonly string $workspaceId,
        public readonly string $projectId,
        public readonly string $pipelineRunId,
        public readonly int $dispatchedAtUnix,
    ) {
        $this->delay = now()->addSeconds(self::DEBOUNCE_SECONDS);
        $this->onQueue('default');
    }

    public function uniqueId(): string
    {
        return "mv_refresh:workspace:{$this->workspaceId}";
    }

    /**
     * Cap how long the unique-lock holds. Should be longer than the
     * delay + execution time so a legitimate run can't be displaced
     * by a sibling, but short enough that a wedged job clears within
     * a few minutes.
     */
    public function uniqueFor(): int
    {
        return 300;
    }

    public function handle(): void
    {
        $stampKey = "mv_refresh:last_dispatch:{$this->workspaceId}";
        $latestDispatchedAt = (int) (Redis::get($stampKey) ?? 0);

        // Coalesce: if a later dispatch happened during our 30s delay,
        // bail — that later job will do the work with fresher data.
        if ($latestDispatchedAt > $this->dispatchedAtUnix) {
            Log::info('mv_refresh.debounce.coalesced', [
                'workspace_id' => $this->workspaceId,
                'this_dispatched_at' => $this->dispatchedAtUnix,
                'latest_dispatched_at' => $latestDispatchedAt,
            ]);

            return;
        }

        $url = rtrim(
            config('services.fastapi.url') ?: env('FASTAPI_INTERNAL_URL', 'http://fastapi:8000'),
            '/',
        ).'/internal/v1/mv-refresh/run';

        $serviceKey = env('FASTAPI_SERVICE_KEY');
        if (! $serviceKey) {
            throw new RuntimeException('FASTAPI_SERVICE_KEY not configured');
        }

        $resp = Http::withHeaders([
            'X-Service-Key' => $serviceKey,
            'Accept' => 'application/json',
        ])
            ->timeout(120)
            ->post($url, [
                'workspace_id' => $this->workspaceId,
                'triggered_by' => 'ingestion',
                'force' => false,
            ]);

        if (! $resp->successful()) {
            throw new RuntimeException(
                'mv_refresh /run returned HTTP '.$resp->status().': '.substr($resp->body(), 0, 200),
            );
        }

        $body = $resp->json();
        $results = $body['results'] ?? [];
        $anyCompleted = collect($results)->contains(fn ($r) => ($r['status'] ?? null) === 'completed');
        $anyFailed = collect($results)->contains(fn ($r) => ($r['status'] ?? null) === 'failed');

        Log::info('mv_refresh.debounce.completed', [
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->projectId,
            'pipeline_run_id' => $this->pipelineRunId,
            'results' => $results,
            'any_completed' => $anyCompleted,
            'any_failed' => $anyFailed,
        ]);

        // Only emit workspace.data_updated when at least one view actually
        // refreshed (or was already fresh) AND nothing failed. A failed
        // refresh means the data is not in a queryable state — emitting
        // the event would tell the frontend to re-fetch stale data.
        if (! $anyFailed) {
            // Phase 4 — read the post-bump silver.projects.data_version
            // so the broadcast carries the version MapView's MVT tile URL
            // cache-bust uses. Done at dispatch time (not job-construction
            // time) so multiple completions that coalesce into one debounced
            // run all surface the same final version.
            //
            // Microsecond-range index scan on the PK; same query the
            // TileProxyController pays for every silver tile request.
            // Falls back to null when the row is unexpectedly absent — the
            // MapView listener treats null as "no new tile-version info".
            $projectDataVersion = $this->fetchProjectDataVersion();

            WorkspaceDataUpdated::dispatch(
                $this->workspaceId,
                $this->projectId,
                $this->pipelineRunId,
                $this->affectedTypesFromResults($results),
                $projectDataVersion,
            );

            // Phase 3 — also fire workspace-level activity for
            // Foundry/Portfolio + Foundry/Projects. The project-scoped
            // WorkspaceDataUpdated above drives the per-project pages;
            // this workspace-scoped event drives the cross-project rollups.
            // Best-effort; failure must not cascade.
            try {
                WorkspaceActivityBroadcast::dispatch(
                    $this->workspaceId,
                    ['projects', 'kpis', 'activity'],
                    [
                        'project_id' => $this->projectId,
                        'pipeline_run_id' => $this->pipelineRunId,
                        'source' => 'ingestion',
                    ],
                );
            } catch (\Throwable $e) {
                Log::warning('mv_refresh.debounce.workspace_activity_failed', [
                    'workspace_id' => $this->workspaceId,
                    'error' => $e->getMessage(),
                ]);
            }

            // Phase 6 — Dashboards/VisualReadiness reads MV-derived
            // viz_coverage rollups. Every successful workspace MV refresh
            // is the natural refresh point for this admin dashboard.
            try {
                AdminSurfaceUpdated::dispatch(
                    'dashboards-visual-readiness',
                    null,
                    ['viz_coverage', 'total_projects'],
                    [
                        'workspace_id' => $this->workspaceId,
                        'project_id' => $this->projectId,
                        'pipeline_run_id' => $this->pipelineRunId,
                    ],
                );
            } catch (\Throwable $e) {
                Log::warning('mv_refresh.debounce.visual_readiness_failed', [
                    'workspace_id' => $this->workspaceId,
                    'error' => $e->getMessage(),
                ]);
            }

            // Phase 6 — record the emission latency (controller dispatch
            // timestamp → broadcast moment) on the FastAPI Prometheus
            // registry via the metric-bridge endpoint. Best-effort.
            $latencySeconds = max(0, time() - $this->dispatchedAtUnix);
            $this->recordEmissionLatency($latencySeconds);
        }
    }

    private function recordEmissionLatency(int $latencySeconds): void
    {
        $serviceKey = env('FASTAPI_SERVICE_KEY');
        if (! $serviceKey) {
            return;
        }
        $url = rtrim(
            config('services.fastapi.url') ?: env('FASTAPI_INTERNAL_URL', 'http://fastapi:8000'),
            '/',
        ).'/internal/v1/metrics/ingestion-event';
        try {
            Http::withHeaders([
                'X-Service-Key' => $serviceKey,
                'Accept' => 'application/json',
            ])->timeout(2)->post($url, [
                'metric' => 'workspace_data_updated_emission_latency_seconds',
                'value' => (float) $latencySeconds,
            ]);
        } catch (\Throwable $e) {
            Log::debug('mv_refresh.debounce.metric_post_failed', [
                'workspace_id' => $this->workspaceId,
                'error' => $e->getMessage(),
            ]);
        }
    }

    /**
     * Read the post-bump silver.projects.data_version for the project that
     * triggered this run. Used to seed the Silver MVT tile cache-bust on
     * the WorkspaceDataUpdated broadcast.
     *
     * Returns null on lookup failure (missing row, DB error). Best-effort;
     * the listener treats null as "no new version info, don't touch tiles".
     */
    private function fetchProjectDataVersion(): ?int
    {
        try {
            $row = DB::selectOne(
                'SELECT data_version FROM silver.projects WHERE project_id = ?::uuid',
                [$this->projectId],
            );

            return $row?->data_version !== null ? (int) $row->data_version : null;
        } catch (\Throwable $e) {
            Log::warning('mv_refresh.debounce.data_version_fetch_failed', [
                'workspace_id' => $this->workspaceId,
                'project_id' => $this->projectId,
                'error' => $e->getMessage(),
            ]);

            return null;
        }
    }

    /**
     * Map refresh results back to high-level affected types so the
     * frontend can do partial reloads scoped to what changed.
     *
     * The MV-derived types (e.g. 'collars' from silver.mv_collar_summary)
     * are accurate per-view. The always-emitted types ('reports', 'quality',
     * 'review_queue') are upstream-side-effect supersets: every ingest_pdf
     * or drill-upload completion writes new rows in silver.reports,
     * silver.document_ingestion_quality, and silver.review_queue, so the
     * Overview / Lakehouse / IngestQuality / DrillReview pages all need to
     * re-fetch. Receiving pages filter on these keys via
     * useWorkspaceDataUpdated; the cost of including a few extra types
     * the receiver doesn't care about is one ignored Echo callback —
     * cheaper than per-table accuracy tracking that adds no UX value.
     *
     * @param array<int, array<string, mixed>> $results
     *
     * @return list<string>
     */
    private function affectedTypesFromResults(array $results): array
    {
        $types = [];
        foreach ($results as $r) {
            $view = (string) ($r['view_name'] ?? '');
            $status = (string) ($r['status'] ?? '');
            if ($status !== 'completed') {
                continue;
            }
            if ($view === 'silver.mv_collar_summary') {
                $types[] = 'collars';
                $types[] = 'assays';
            }
        }
        // Upstream-side-effect superset — see method docblock.
        $types[] = 'reports';
        $types[] = 'quality';
        $types[] = 'review_queue';
        // Phase 5 additions — symmetry with the existing superset. The
        // `hypotheses` type fires `Foundry/Reasoning` reloads; `what_changed`
        // fires `Foundry/WhatChangedFeed`. Receivers filter on these keys
        // in their useWorkspaceDataUpdated callback. Cost of one extra
        // ignored Echo callback per page < cost of per-type accuracy tracking.
        $types[] = 'hypotheses';
        $types[] = 'what_changed';

        return array_values(array_unique($types));
    }
}
