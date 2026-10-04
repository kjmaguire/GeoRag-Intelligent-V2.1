<?php

declare(strict_types=1);

namespace App\Jobs;

use App\Events\Admin\AdminSurfaceUpdated;
use App\Events\Workspace\WorkspaceActivityBroadcast;
use App\Events\WorkspaceDataUpdated;
use App\Http\Controllers\Internal\IngestionProgressBroadcastController;
use Illuminate\Contracts\Queue\ShouldQueue;
use Illuminate\Foundation\Queue\Queueable;
use Illuminate\Queue\InteractsWithQueue;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Facades\Redis;
use Illuminate\Support\Str;
use RuntimeException;

/**
 * Phase 2 of the reliability spec — debounced per-workspace MV refresh.
 *
 * Dispatched through {@see self::debounce()} from
 * {@see IngestionProgressBroadcastController} whenever a completed-status
 * ingestion event lands. A burst of completions for one workspace coalesces
 * into exactly ONE refresh, run DEBOUNCE_SECONDS after the LAST completion of
 * the burst (trailing-edge debounce) — the Phase 1 Ontario Gold re-ingest
 * fired 9 completions inside a minute, and we don't want to pay 9× REFRESH
 * cost when one will do.
 *
 * How it coalesces (rewritten 2026-09-29, LAR-3):
 *
 *   - Every completion queues its own delayed job carrying a fresh random
 *     `dispatchToken`, and then writes that token to a per-workspace Redis
 *     stamp. The stamp therefore always names the newest job.
 *   - At handle() a job whose token is no longer the stamp bails: a newer
 *     job exists and will run after its own delay. The newest job finds its
 *     own token and does the work. Exactly one runs per burst.
 *
 * Why not ShouldBeUnique any more: the old design had BOTH a unique lock and
 * this stamp check, and they cancelled out. The lock (held from dispatch
 * until the job finished) rejected completion B's dispatch inside A's delay;
 * then A saw B's newer stamp and bailed for a job the lock had refused to
 * queue. Any two completions within 30 s produced ZERO refreshes and no
 * WorkspaceDataUpdated, so open pages never live-reloaded after a multi-file
 * import. ShouldBeUniqueUntilProcessing would not fix it either — A still
 * holds the lock while B dispatches during A's delay.
 *
 * Every failure mode leans toward refreshing, never toward silence:
 *   - Redis unreadable at handle() → run.
 *   - Stamp expired or missing → run.
 *   - Dispatch succeeds but the stamp write fails → the older job still sees
 *     its own token and runs (the stamp is written AFTER dispatch for this
 *     reason; the reverse order could supersede a job with one never queued).
 * The cost of those paths is at most a redundant refresh, which FastAPI's
 * per-view advisory try-lock makes cheap.
 *
 * Trailing-edge means a completion stream that never pauses for
 * DEBOUNCE_SECONDS defers the refresh until it does. The 18:00 UTC
 * `mv_refresh_silver` Hatchet cron is the backstop either way.
 *
 * Timeouts (LAR-7): the job runs on supervisor-1, whose worker timeout is
 * 60 s (config/horizon.php). The FastAPI call used to wait 120 s, so a slow
 * refresh got the worker SIGKILLed mid-request, left `reserved` until
 * retry_after, and was retried into the same kill three times. The HTTP
 * timeout is now HTTP_TIMEOUT_SECONDS (< $timeout), so a slow refresh fails
 * the attempt cleanly and is retried after $backoff; the retry either finds
 * the view refreshed or skips it under FastAPI's advisory lock.
 * tests/Unit/Jobs/DebounceWorkspaceMvRefreshTimeoutTest.php pins the order.
 *
 * When the job decides to run, it POSTs to FastAPI's
 * /internal/v1/mv-refresh/run endpoint, which performs the actual
 * REFRESH under per-view advisory locks + logs to gold.mv_refresh_log.
 *
 * After the refresh the job dispatches a {@see WorkspaceDataUpdated} event so
 * the frontend pages (Overview/Lakehouse/Drillhole/Map) know to re-fetch their
 * data. It does so even when a view FAILED to refresh (the non-MV tables have
 * new rows regardless; the failed view's own types are left out of the
 * payload) and then throws, so the queue retries the MV part under $tries /
 * $backoff instead of dropping it.
 *
 * Octane: nothing here is resident — the job is constructed per dispatch and
 * debounce() is a static function with no static state.
 */
class DebounceWorkspaceMvRefresh implements ShouldQueue
{
    use InteractsWithQueue;
    use Queueable;

    /** Refresh debounce window (quiet period after the last completion). */
    public const DEBOUNCE_SECONDS = 30;

    /**
     * Upper bound on the FastAPI /mv-refresh/run call. Must stay below
     * {@see $timeout}, which must not exceed supervisor-1's worker timeout.
     */
    public const HTTP_TIMEOUT_SECONDS = 45;

    public const HTTP_CONNECT_TIMEOUT_SECONDS = 5;

    /**
     * How long the "newest dispatch" stamp outlives the burst. Long enough
     * that a Horizon backlog cannot expire it before the burst's jobs run;
     * if it does expire, every waiting job runs (redundant, never zero).
     */
    public const STAMP_TTL_SECONDS = 3600;

    public int $tries = 3;

    public int $backoff = 30;

    /** Matches supervisor-1's worker timeout; see the class docblock. */
    public int $timeout = 60;

    /**
     * Identifies this dispatch in the per-workspace stamp. A plain property
     * with a default (not a promoted readonly one) so a job serialized before
     * this field existed unserializes with null instead of an uninitialized
     * property; null takes the legacy timestamp comparison in handle().
     */
    public ?string $dispatchToken = null;

    public function __construct(
        public readonly string $workspaceId,
        public readonly string $projectId,
        public readonly string $pipelineRunId,
        public readonly int $dispatchedAtUnix,
        ?string $dispatchToken = null,
    ) {
        $this->dispatchToken = $dispatchToken;
        $this->delay = now()->addSeconds(self::DEBOUNCE_SECONDS);
        $this->onQueue('default');
    }

    /**
     * Record a completion and queue the trailing-edge refresh for it.
     *
     * The only supported entry point: dispatching the job directly skips the
     * stamp, and a job whose token was never stamped runs unconditionally.
     */
    public static function debounce(string $workspaceId, string $projectId, string $pipelineRunId): void
    {
        $token = (string) Str::uuid();

        self::dispatch($workspaceId, $projectId, $pipelineRunId, time(), $token);

        Redis::setex(self::stampKey($workspaceId), self::STAMP_TTL_SECONDS, $token);
    }

    public static function stampKey(string $workspaceId): string
    {
        return "mv_refresh:last_dispatch:{$workspaceId}";
    }

    public function handle(): void
    {
        $supersededBy = $this->supersedingDispatch();
        if ($supersededBy !== null) {
            Log::info('mv_refresh.debounce.coalesced', [
                'workspace_id' => $this->workspaceId,
                'this_dispatch' => $this->dispatchToken ?? $this->dispatchedAtUnix,
                'latest_dispatch' => $supersededBy,
            ]);

            return;
        }

        // `services.fastapi.url` never existed -- the keys are
        // `internal_url` and `base_url` -- so this always fell through to the
        // bare env() read that config/services.php's own comment says the
        // indirection is there to avoid. Under a cached config that read
        // returns the literal default, and `http://fastapi:8000` is a
        // docker-compose service name that resolves to nothing on Container
        // Apps. The MV refresh is the only thing that emits
        // WorkspaceDataUpdated after an ingest, so the failure mode is a
        // workspace whose pages never notice new data.
        $url = rtrim((string) config('services.fastapi.internal_url'), '/')
            .'/internal/v1/mv-refresh/run';

        $serviceKey = config('services.fastapi.service_key');
        if (! $serviceKey) {
            throw new RuntimeException('FASTAPI_SERVICE_KEY not configured');
        }

        // X-Workspace-Id keys FastAPI's rate limiter per tenant for this
        // JWT-less service call; without it every workspace's refresh
        // shares the one bucket keyed on this worker's IP.
        $resp = Http::withHeaders([
            'X-Service-Key' => $serviceKey,
            'X-Workspace-Id' => $this->workspaceId,
            'Accept' => 'application/json',
        ])
            ->connectTimeout(self::HTTP_CONNECT_TIMEOUT_SECONDS)
            ->timeout(self::HTTP_TIMEOUT_SECONDS)
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

        // Emit workspace.data_updated whether or not a view failed. A failed
        // MV refresh used to suppress the event entirely and the job did not
        // retry, so open pages never reloaded after an ingest even though the
        // non-MV tables (reports, quality, review_queue, structures, curves,
        // ...) had new rows. affectedTypesFromResults() only names the
        // MV-derived types (collars/assays) for views that COMPLETED, so a
        // failed view's types are simply absent from the payload: the pages
        // reload what is queryable and do not present a stale MV as fresh.
        //
        // `$anyCompleted` is logged, not gated on: on an empty results array
        // the honest reading is "nothing to refresh" and one redundant partial
        // reload costs less than suppressing a real update.
        //
        // Only once per failing burst: a retry whose view failed again would
        // re-broadcast an identical event (and re-trigger every open page's
        // reload) on each of $tries attempts. The first attempt already told
        // the pages about the non-MV rows; a retry emits only when it
        // succeeds, because then the previously-failed view's types are newly
        // fresh. attempts() is 1 when there is no queue job (direct call).
        if (! $anyFailed || $this->attempts() === 1) {
            $this->emitDataUpdated($results);
        }

        if ($anyFailed) {
            // Throw AFTER emitting so the queue retries the MV part ($tries /
            // $backoff). The retry either finds the view refreshed or skips it
            // under FastAPI's per-view advisory lock; once the attempts are
            // exhausted the job lands in failed_jobs for an operator, which is
            // louder than the silent success this used to be. The 18:00 UTC
            // mv_refresh_silver Hatchet cron remains the final backstop.
            throw new RuntimeException(
                'mv_refresh: one or more views failed to refresh for workspace '.$this->workspaceId
                .'; non-MV data update was broadcast, retrying the refresh.',
            );
        }

        // Phase 6 — Dashboards/VisualReadiness reads MV-derived viz_coverage
        // rollups, so it is only told to refresh when every view refreshed.
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

    /**
     * Broadcast the project-scoped and workspace-scoped "data updated" events.
     *
     * @param array<int, array<string, mixed>> $results
     */
    private function emitDataUpdated(array $results): void
    {
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
    }

    /**
     * The stamp value of a NEWER dispatch for this workspace, or null when
     * this job is the newest (or the stamp cannot be read — fail toward
     * refreshing, never toward silence).
     */
    private function supersedingDispatch(): ?string
    {
        try {
            $latest = Redis::get(self::stampKey($this->workspaceId));
        } catch (\Throwable $e) {
            Log::warning('mv_refresh.debounce.stamp_unreadable', [
                'workspace_id' => $this->workspaceId,
                'error' => $e->getMessage(),
            ]);

            return null;
        }

        if ($latest === null || $latest === false || $latest === '') {
            return null;
        }
        $latest = (string) $latest;

        if ($this->dispatchToken !== null) {
            return $latest === $this->dispatchToken ? null : $latest;
        }

        // Legacy job, queued before dispatch tokens existed: its stamp was a
        // unix timestamp. A non-numeric stamp is a token written by a newer
        // dispatch, which will run itself.
        if (! is_numeric($latest)) {
            return $latest;
        }

        return (int) $latest > $this->dispatchedAtUnix ? $latest : null;
    }

    private function recordEmissionLatency(int $latencySeconds): void
    {
        $serviceKey = config('services.fastapi.service_key');
        if (! $serviceKey) {
            return;
        }
        $url = rtrim((string) config('services.fastapi.internal_url'), '/')
            .'/internal/v1/metrics/ingestion-event';
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
        // 2026-08-22 — the two data shapes that had no type at all.
        // ingest_spatial writes silver.spatial_features (structures, faults,
        // contacts) and ingest_well_logs writes curves; neither is derived
        // from silver.mv_collar_summary, so neither appeared in this list.
        // Foundry/DrillholeDetail filtered on collars|assays and therefore
        // sat on a stale strip log after a LAS upload and a stale structure
        // set after a shapefile upload — the two uploads whose whole point
        // is that page.
        $types[] = 'structures';
        $types[] = 'curves';
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
