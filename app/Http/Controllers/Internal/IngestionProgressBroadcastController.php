<?php

declare(strict_types=1);

namespace App\Http\Controllers\Internal;

use App\Events\IngestionProgressBroadcast;
use App\Http\Controllers\Controller;
use App\Jobs\DebounceWorkspaceMvRefresh;
use App\Services\Ingestion\WorkspaceDataVersionBumper;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Facades\Redis;

/**
 * Internal — FastAPI → Laravel bridge for ingestion progress events.
 *
 * Service-key auth only. FastAPI POSTs here from:
 *   - ingest_pdf.on_failure_task (status = failed | cancelled)
 *   - ingest_pdf.embed_verify    (status = completed)
 *   - stale_run_detector cron    (status = timed_out)
 *
 * Phase 1 — always dispatch IngestionProgressBroadcast so IngestionRuns
 * UI flips immediately.
 *
 * Phase 2 — on `status='completed'` ONLY, additionally:
 *   1. Bump silver.workspaces.data_version + silver.projects.data_version
 *      via {@see WorkspaceDataVersionBumper} (Redis SETNX-guarded, so
 *      Hatchet retries can't double-bump).
 *   2. Stamp a per-workspace "last dispatch" timestamp in Redis so the
 *      debounced MV refresh job can detect stale predecessors.
 *   3. Dispatch {@see DebounceWorkspaceMvRefresh} (delayed 30s, unique
 *      per workspace, coalesces bursts).
 *
 * The debounce job itself dispatches WorkspaceDataUpdated once the
 * refresh succeeds — never from this controller, because the data
 * isn't queryable until the MV is fresh.
 */
class IngestionProgressBroadcastController extends Controller
{
    public function broadcast(Request $request, WorkspaceDataVersionBumper $bumper): JsonResponse
    {
        $payload = $request->validate([
            'workspace_id' => ['required', 'uuid'],
            'project_id' => ['required', 'uuid'],
            'pipeline_run_id' => ['required', 'uuid'],
            'stage' => ['required', 'string', 'max:60'],
            'status' => ['required', 'string', 'in:queued,started,completed,failed,cancelled,timed_out'],
            'message' => ['nullable', 'string', 'max:500'],
            'pct' => ['nullable', 'integer', 'min:0', 'max:100'],
        ]);

        IngestionProgressBroadcast::dispatch(
            $payload['workspace_id'],
            $payload['project_id'],
            $payload['pipeline_run_id'],
            $payload['stage'],
            $payload['status'],
            $payload['message'] ?? null,
            $payload['pct'] ?? null,
        );

        $sideEffects = ['data_version_bumped' => false, 'mv_refresh_dispatched' => false];

        if ($payload['status'] === 'completed') {
            $bump = $bumper->bump(
                $payload['workspace_id'],
                $payload['project_id'],
                $payload['pipeline_run_id'],
            );
            $sideEffects['data_version_bumped'] = $bump['bumped'];
            $sideEffects['data_version'] = $bump['workspace_version'];

            // Stamp the last-dispatch time so an already-queued debounce
            // job whose delay window predates this dispatch can coalesce
            // itself (see DebounceWorkspaceMvRefresh::handle).
            $now = time();
            Redis::setex(
                "mv_refresh:last_dispatch:{$payload['workspace_id']}",
                600,
                (string) $now,
            );

            // Unique-job dispatch: if another DebounceWorkspaceMvRefresh
            // for this workspace is already queued or running, this is a
            // no-op (ShouldBeUnique). When the existing job runs, it
            // will read the fresh last_dispatch stamp and pick up this
            // completion's work.
            DebounceWorkspaceMvRefresh::dispatch(
                $payload['workspace_id'],
                $payload['project_id'],
                $payload['pipeline_run_id'],
                $now,
            );

            $sideEffects['mv_refresh_dispatched'] = true;
        }

        Log::info('ingestion.progress.broadcast', [
            'workspace_id' => $payload['workspace_id'],
            'project_id' => $payload['project_id'],
            'pipeline_run_id' => $payload['pipeline_run_id'],
            'status' => $payload['status'],
            'stage' => $payload['stage'],
            'side_effects' => $sideEffects,
        ]);

        return response()->json(['ok' => true, 'side_effects' => $sideEffects]);
    }
}
