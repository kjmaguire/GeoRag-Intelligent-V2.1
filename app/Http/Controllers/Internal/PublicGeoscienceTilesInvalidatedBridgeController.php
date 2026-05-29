<?php

declare(strict_types=1);

namespace App\Http\Controllers\Internal;

use App\Events\Map\PublicGeoscienceTilesInvalidated;
use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Log;

/**
 * Internal — FastAPI → Laravel bridge for Public-Geoscience tile invalidation.
 *
 * Service-key auth only. Dispatches
 * {@see App\Events\Map\PublicGeoscienceTilesInvalidated} on
 * `public-geoscience.tiles`; the browser-side PublicGeoscienceMap re-issues
 * MapLibre setTiles() against every (or a subset of) PGEO source with the
 * new ?v={epoch} cache-bust.
 *
 * Sibling endpoints (Phase 1–4 bridge family):
 *   - /api/internal/v1/ingest-progress/broadcast              (ingestion lifecycle, project)
 *   - /api/internal/v1/workspace-data-updated                 (non-ingestion project event)
 *   - /api/internal/v1/admin-surface-updated                  (admin tier surfaces)
 *   - /api/internal/v1/workspace-activity                     (workspace cross-project)
 *   - /api/internal/v1/user-inbox-updated                     (per-user inbox)
 *   - /api/internal/v1/public-geoscience-tiles-invalidated    (this — global PGEO tiles)
 *
 * Distinct from a workspace.{ws}.activity event with affected_types=['pgeo']
 * because PGEO is a workspace-global corpus — every authenticated user
 * sees the same data. A workspace-scoped channel would miss multi-tenant
 * subscribers; this dedicated channel is gated on $user !== null only.
 */
class PublicGeoscienceTilesInvalidatedBridgeController extends Controller
{
    public function broadcast(Request $request): JsonResponse
    {
        $payload = $request->validate([
            'jurisdiction_epoch' => ['required', 'integer', 'min:0'],
            'source_ids' => ['nullable', 'array'],
            'source_ids.*' => ['string', 'max:60'],
        ]);

        PublicGeoscienceTilesInvalidated::dispatch(
            (int) $payload['jurisdiction_epoch'],
            $payload['source_ids'] ?? null,
        );

        Log::info('public_geoscience.tiles_invalidated.broadcast', [
            'jurisdiction_epoch' => $payload['jurisdiction_epoch'],
            'source_ids' => $payload['source_ids'] ?? null,
        ]);

        return response()->json(['ok' => true]);
    }
}
