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
use Throwable;

/**
 * Internal — FastAPI → Laravel bridge for ingestion progress events.
 *
 * Service-key auth only. FastAPI POSTs here from:
 *   - ingest_pdf.on_failure_task (status = failed | cancelled)
 *   - ingest_pdf.embed_verify    (status = completed)
 *   - stale_run_detector cron    (status = timed_out)
 *   - ingest_tabular / ingest_spatial / ingest_well_logs, on reaching a
 *     terminal state (status = completed | partial). Added 2026-08-22:
 *     those three never called here at all, so drill CSVs, shapefiles and
 *     LAS files finished into silence — the run went terminal in Postgres
 *     and no product surface heard about it.
 *
 * Phase 1 — always dispatch IngestionProgressBroadcast so IngestionRuns
 * UI flips immediately.
 *
 * Phase 2 — on a terminal status that WROTE something (`completed` or
 * `partial`), additionally:
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
    /**
     * Terminal statuses that mean rows reached Silver, so the read side is
     * now stale: bump data_version and queue the MV refresh.
     *
     * Deliberately not "every terminal status" — failed / cancelled /
     * timed_out wrote nothing worth invalidating a tile cache for.
     */
    public const DATA_LANDED_STATUSES = ['completed', 'partial'];

    public function broadcast(Request $request, WorkspaceDataVersionBumper $bumper): JsonResponse
    {
        $payload = $request->validate([
            'workspace_id' => ['required', 'uuid'],
            'project_id' => ['required', 'uuid'],
            'pipeline_run_id' => ['required', 'uuid'],
            'stage' => ['required', 'string', 'max:60'],
            // 'partial' is a real terminal status in silver.ingest_progress
            // (the CHECK constraint has carried it since 2026_08_21) — a run
            // that reached the end, wrote rows, and also had something to
            // say. It was missing here, so the workflows that produce it
            // could not report themselves finished at all.
            'status' => ['required', 'string', 'in:queued,started,completed,partial,failed,cancelled,timed_out'],
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

        // 'partial' gets the same side effects as 'completed'. A partial run
        // is a run that WROTE something and also had a complaint — the rows
        // are in Silver either way, so the tiles are stale either way, and
        // refusing to bump would leave exactly the ingests with problems as
        // the ones the map never shows.
        if (in_array($payload['status'], self::DATA_LANDED_STATUSES, true)) {
            $bump = $bumper->bump(
                $payload['workspace_id'],
                $payload['project_id'],
                $payload['pipeline_run_id'],
            );
            $sideEffects['data_version_bumped'] = $bump['bumped'];
            $sideEffects['data_version'] = $bump['workspace_version'];

            // The MV-refresh debounce is a secondary optimisation, and it is
            // the only part of this endpoint that needs Redis. Guarded for
            // the same reason WorkspaceDataVersionBumper::bump() is: an
            // unreachable Redis threw `RedisException: Connection refused`
            // straight out of the controller and returned 500 to FastAPI's
            // progress callback.
            //
            // That is the worst possible response to this particular outage.
            // The primary job here — recording terminal progress so the UI
            // can stop showing a spinner — is a Postgres write that already
            // succeeded above. Turning a degraded refresh into a failed
            // callback means the run looks stuck forever instead of
            // completing with a slightly stale materialised view.
            //
            // So: report what did not happen, return 200, and let the
            // nightly MV refresh catch up.
            try {
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
            } catch (Throwable $e) {
                $sideEffects['mv_refresh_dispatched'] = false;
                $sideEffects['mv_refresh_error'] = $e->getMessage();
                Log::warning('ingestion.progress.mv_refresh_dispatch_failed', [
                    'workspace_id' => $payload['workspace_id'],
                    'project_id' => $payload['project_id'],
                    'pipeline_run_id' => $payload['pipeline_run_id'],
                    'error' => $e->getMessage(),
                ]);
            }
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
