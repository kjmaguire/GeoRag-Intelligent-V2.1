<?php

declare(strict_types=1);

namespace App\Http\Controllers\Internal;

use App\Events\WorkspaceDataUpdated;
use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Log;

/**
 * Internal — FastAPI → Laravel bridge for non-ingestion workspace updates.
 *
 * Service-key auth only. Symmetric to
 * {@see IngestionProgressBroadcastController} but without the MV-refresh
 * / data_version-bump side-effects: this endpoint is for workflows whose
 * completion writes project-scoped tables that DON'T need a materialized-
 * view refresh and DON'T bump silver.workspaces.data_version (because
 * the underlying data is already queryable the moment the workflow's own
 * commit lands).
 *
 * Current callers:
 *   - score_targets.execute (on success) — writes
 *     targeting.target_recommendations directly; the Foundry/Targets page
 *     reads from that table, no MV in the path.
 *
 * Future callers (per audit):
 *   - any project-scoped workflow whose output the SPA reads via a
 *     non-MV-backed Inertia controller.
 *
 * For ingestion (ingest_pdf / drill-upload), keep using
 * /api/internal/v1/ingest-progress/broadcast — it does the data_version
 * bump + debounced MV refresh + emits WorkspaceDataUpdated from the job
 * AFTER refresh confirms. This endpoint emits WorkspaceDataUpdated
 * directly (no debounce, no refresh) because there's nothing to refresh.
 */
class WorkspaceDataUpdatedBridgeController extends Controller
{
    /**
     * Dispatch a WorkspaceDataUpdated event from FastAPI.
     *
     * Request body shape (JSON):
     *   workspace_id    UUID
     *   project_id      UUID
     *   pipeline_run_id UUID
     *   affected_types  list<string>  — keys for partial-reload routing
     *                                   on the receiving page; see the
     *                                   useWorkspaceDataUpdated hook.
     */
    public function broadcast(Request $request): JsonResponse
    {
        $payload = $request->validate([
            'workspace_id' => ['required', 'uuid'],
            'project_id' => ['required', 'uuid'],
            'pipeline_run_id' => ['required', 'uuid'],
            'affected_types' => ['required', 'array', 'min:1'],
            'affected_types.*' => ['string', 'max:60'],
        ]);

        WorkspaceDataUpdated::dispatch(
            $payload['workspace_id'],
            $payload['project_id'],
            $payload['pipeline_run_id'],
            $payload['affected_types'],
        );

        Log::info('workspace.data_updated.broadcast', [
            'workspace_id' => $payload['workspace_id'],
            'project_id' => $payload['project_id'],
            'pipeline_run_id' => $payload['pipeline_run_id'],
            'affected_types' => $payload['affected_types'],
        ]);

        return response()->json(['ok' => true]);
    }
}
