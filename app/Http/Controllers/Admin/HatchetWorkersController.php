<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin;

use App\Http\Controllers\Controller;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Phase 1 Step 7 — Hatchet Worker Dashboard.
 *
 * Read-only admin surface that surfaces the Hatchet engine's own state:
 * which workers are alive, what each is registered for, and how its
 * recent runs have classified. The dashboard reads two databases:
 *
 *   - ``pgsql`` (georag) — application + classification rollups
 *   - ``pgsql_hatchet`` (hatchet) — engine state ("Worker", "WorkflowRun",
 *     "WorkflowVersion", "Workflow")
 *
 * Auth: 'admin' Gate (users.is_admin = true).
 *
 * Route: GET /admin/hatchet-workers
 */
class HatchetWorkersController extends Controller
{
    /** Workers older than this without a heartbeat are listed but flagged stale. */
    private const STALE_HEARTBEAT_SECONDS = 60;

    /** Workers older than this are hidden from the live count. */
    private const ALIVE_HEARTBEAT_SECONDS = 90;

    public function index(Request $request): Response
    {
        $this->authorize('admin');

        return Inertia::render('Admin/HatchetWorkers', [
            'pools' => $this->poolRollup(),
            'workflows' => $this->registeredWorkflows(),
            'recent_runs' => $this->recentRunsRollup(),
            'engine_health' => $this->engineHealth(),
        ]);
    }

    /**
     * Per-pool worker counts. Pool name is derived from the worker's
     * ``name`` column (we deploy them as ``georag-hatchet-worker-<pool>``).
     *
     * @return array<int, array{
     *   name: string,
     *   live: int,
     *   stale: int,
     *   total_history: int,
     *   max_runs: int,
     *   last_heartbeat_at: ?string,
     * }>
     */
    private function poolRollup(): array
    {
        $rows = DB::connection('pgsql_hatchet')->select(
            <<<'SQL'
            SELECT name,
                   max("maxRuns") AS max_runs,
                   max("lastHeartbeatAt") AS last_heartbeat_at,
                   count(*) FILTER (WHERE "isActive" AND
                       "lastHeartbeatAt" > now() - interval '90 seconds')   AS live,
                   count(*) FILTER (WHERE "isActive" AND
                       "lastHeartbeatAt" <= now() - interval '90 seconds'
                       AND "lastHeartbeatAt" > now() - interval '7 days')   AS stale,
                   count(*) AS total_history
            FROM "Worker"
            WHERE "deletedAt" IS NULL
            GROUP BY name
            ORDER BY name
            SQL
        );

        return array_map(static fn (object $r) => [
            'name' => $r->name,
            'live' => (int) $r->live,
            'stale' => (int) $r->stale,
            'total_history' => (int) $r->total_history,
            'max_runs' => (int) $r->max_runs,
            'last_heartbeat_at' => $r->last_heartbeat_at,
        ], $rows);
    }

    /**
     * Engine-side workflow registry — one row per current ``Workflow`` name.
     *
     * @return array<int, array{name: string, version_count: int, latest_version_at: ?string}>
     */
    private function registeredWorkflows(): array
    {
        $rows = DB::connection('pgsql_hatchet')->select(
            <<<'SQL'
            SELECT w.name,
                   count(v.id) AS version_count,
                   max(v."createdAt") AS latest_version_at
            FROM "Workflow" w
            LEFT JOIN "WorkflowVersion" v
                ON v."workflowId" = w.id
               AND v."deletedAt" IS NULL
            WHERE w."deletedAt" IS NULL
            GROUP BY w.name
            ORDER BY w.name
            SQL
        );

        return array_map(static fn (object $r) => [
            'name' => $r->name,
            'version_count' => (int) $r->version_count,
            'latest_version_at' => $r->latest_version_at,
        ], $rows);
    }

    /**
     * Last 24h of WorkflowRuns grouped by workflow name + status.
     *
     * @return array<int, array{
     *   workflow_name: string,
     *   succeeded: int,
     *   failed: int,
     *   running: int,
     *   queued: int,
     *   cancelled: int,
     *   p50_duration_ms: ?int,
     *   p95_duration_ms: ?int,
     *   last_started_at: ?string,
     * }>
     */
    private function recentRunsRollup(): array
    {
        $rows = DB::connection('pgsql_hatchet')->select(
            <<<'SQL'
            WITH last24 AS (
                SELECT wr.id,
                       wr.status,
                       wr.duration,
                       wr."startedAt",
                       w.name AS workflow_name
                FROM "WorkflowRun" wr
                JOIN "WorkflowVersion" v ON v.id = wr."workflowVersionId"
                JOIN "Workflow"        w ON w.id = v."workflowId"
                WHERE wr."deletedAt" IS NULL
                  AND wr."createdAt" > now() - interval '24 hours'
            )
            SELECT workflow_name,
                   count(*) FILTER (WHERE status = 'SUCCEEDED')          AS succeeded,
                   count(*) FILTER (WHERE status = 'FAILED')             AS failed,
                   count(*) FILTER (WHERE status = 'RUNNING')            AS running,
                   count(*) FILTER (WHERE status IN ('QUEUED','PENDING'))
                                                                          AS queued,
                   count(*) FILTER (WHERE status = 'CANCELLED')          AS cancelled,
                   percentile_disc(0.5)
                       WITHIN GROUP (ORDER BY duration) FILTER (WHERE duration IS NOT NULL)
                                                                          AS p50_duration_ms,
                   percentile_disc(0.95)
                       WITHIN GROUP (ORDER BY duration) FILTER (WHERE duration IS NOT NULL)
                                                                          AS p95_duration_ms,
                   max("startedAt")                                       AS last_started_at
            FROM last24
            GROUP BY workflow_name
            ORDER BY workflow_name
            SQL
        );

        return array_map(static fn (object $r) => [
            'workflow_name' => $r->workflow_name,
            'succeeded' => (int) $r->succeeded,
            'failed' => (int) $r->failed,
            'running' => (int) $r->running,
            'queued' => (int) $r->queued,
            'cancelled' => (int) $r->cancelled,
            'p50_duration_ms' => $r->p50_duration_ms !== null ? (int) $r->p50_duration_ms : null,
            'p95_duration_ms' => $r->p95_duration_ms !== null ? (int) $r->p95_duration_ms : null,
            'last_started_at' => $r->last_started_at,
        ], $rows);
    }

    /**
     * @return array{
     *   tenant_count: int,
     *   active_workflow_count: int,
     *   total_workers_24h: int,
     *   live_workers_now: int,
     * }
     */
    private function engineHealth(): array
    {
        $row = DB::connection('pgsql_hatchet')->selectOne(
            <<<'SQL'
            SELECT
              (SELECT count(*) FROM "Tenant"   WHERE "deletedAt" IS NULL)                 AS tenant_count,
              (SELECT count(*) FROM "Workflow" WHERE "deletedAt" IS NULL)                 AS active_workflow_count,
              (SELECT count(DISTINCT id) FROM "Worker"
                 WHERE "deletedAt" IS NULL
                   AND "lastHeartbeatAt" > now() - interval '24 hours')                   AS total_workers_24h,
              (SELECT count(DISTINCT id) FROM "Worker"
                 WHERE "deletedAt" IS NULL
                   AND "isActive"
                   AND "lastHeartbeatAt" > now() - interval '90 seconds')                 AS live_workers_now
            SQL
        );

        return [
            'tenant_count' => (int) ($row->tenant_count ?? 0),
            'active_workflow_count' => (int) ($row->active_workflow_count ?? 0),
            'total_workers_24h' => (int) ($row->total_workers_24h ?? 0),
            'live_workers_now' => (int) ($row->live_workers_now ?? 0),
        ];
    }
}
