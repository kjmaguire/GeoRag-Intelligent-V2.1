<?php

declare(strict_types=1);

namespace App\Http\Controllers\Internal;

use App\Events\Workspace\WorkspaceActivityBroadcast;
use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Log;

/**
 * Internal — FastAPI / Hatchet → Laravel bridge for workspace-level activity.
 *
 * Service-key auth only. Dispatches
 * {@see App\Events\Workspace\WorkspaceActivityBroadcast} on the
 * `workspace.{workspaceId}.activity` private channel; the receiving SPA
 * page filters by `affected_types` and calls
 * `router.reload({ only: [...] })`.
 *
 * Sibling endpoints (Phase 1–3 bridge family):
 *   - /api/internal/v1/ingest-progress/broadcast       — project-scoped ingestion lifecycle
 *   - /api/internal/v1/workspace-data-updated          — project-scoped non-ingestion (score_targets, etc.)
 *   - /api/internal/v1/admin-surface-updated           — admin-tier surfaces (Phase 2)
 *   - /api/internal/v1/user-inbox-updated              — per-user inbox (Phase 3)
 *   - /api/internal/v1/workspace-activity              — this endpoint, workspace-scoped
 *
 * Use this when a single workspace has many projects and a writer affects
 * the workspace-level aggregate (Portfolio KPIs, Projects index, etc.).
 * For single-project writes prefer post_workspace_data_updated so the
 * partial reload stays focused.
 */
class WorkspaceActivityBridgeController extends Controller
{
    public function broadcast(Request $request): JsonResponse
    {
        $payload = $request->validate([
            'workspace_id' => ['required', 'uuid'],
            'affected_types' => ['required', 'array', 'min:1'],
            'affected_types.*' => ['string', 'max:60'],
            'payload' => ['nullable', 'array'],
        ]);

        WorkspaceActivityBroadcast::dispatch(
            $payload['workspace_id'],
            $payload['affected_types'],
            $payload['payload'] ?? [],
        );

        Log::info('workspace.activity.broadcast', [
            'workspace_id' => $payload['workspace_id'],
            'affected_types' => $payload['affected_types'],
            'payload_keys' => array_keys($payload['payload'] ?? []),
        ]);

        return response()->json(['ok' => true]);
    }
}
