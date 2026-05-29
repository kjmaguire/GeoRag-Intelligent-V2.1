<?php

declare(strict_types=1);

namespace App\Events\Map;

use Illuminate\Broadcasting\InteractsWithSockets;
use Illuminate\Broadcasting\PrivateChannel;
use Illuminate\Contracts\Broadcasting\ShouldBroadcastNow;
use Illuminate\Foundation\Events\Dispatchable;
use Illuminate\Queue\SerializesModels;

/**
 * Public-Geoscience tile cache invalidation — Phase 4 of the real-time
 * staleness fix.
 *
 * Fires when {@see public_geoscience_pull} (Hatchet) — or the SMDI
 * overnight pipeline (P3 follow-up) — successfully ingests new
 * public_geo.* data. The browser-side PublicGeoscienceMap subscribes
 * to this event and re-issues setTiles() on every PGEO source with the
 * new ?v={epoch} cache-bust, forcing MapLibre to drop its in-memory
 * tile cache and refetch.
 *
 * Channel: private-public-geoscience.tiles
 *   Auth: any authenticated user (matches the route auth on
 *   /tiles/public-geoscience/*). The PGEO map is a workspace-global
 *   read-only corpus — no per-project scoping.
 *
 * Payload semantics:
 *   - jurisdictionEpoch: EXTRACT(EPOCH FROM MAX(updated_at))::bigint
 *     from public_geo.jurisdictions. Matches the same value
 *     TileProxyController::computePgeoEtag uses for the server-side
 *     ETag, so the client cache-bust and the server ETag stay in
 *     lockstep — when this event fires, the next setTiles() request
 *     skips the browser HTTP cache AND finds a different ETag at the
 *     proxy → real tile re-render.
 *
 *   - sourceIds (optional): subset of LAYER_SPECS ids to invalidate.
 *     Null = invalidate every PGEO source. Useful when a specific
 *     workflow (e.g. SMDI) only mutates one view — limits the
 *     setTiles() churn.
 *
 * Distinct from the Phase 1/2/3 events:
 *   - WorkspaceDataUpdated (Phase 1) — project-scoped Silver invalidation.
 *   - WorkspaceActivityBroadcast (Phase 3) — workspace-level page-prop drift.
 *   - This event — global PGEO tile cache.
 */
class PublicGeoscienceTilesInvalidated implements ShouldBroadcastNow
{
    use Dispatchable;
    use InteractsWithSockets;
    use SerializesModels;

    /**
     * @param ?list<string> $sourceIds Specific PGEO source IDs (e.g. ['pg_mines', 'pg_drillhole_collars']); null = all.
     */
    public function __construct(
        public readonly int $jurisdictionEpoch,
        public readonly ?array $sourceIds = null,
    ) {}

    public function broadcastOn(): array
    {
        return [new PrivateChannel('public-geoscience.tiles')];
    }

    public function broadcastAs(): string
    {
        return 'public_geoscience.tiles_invalidated';
    }

    /**
     * @return array{
     *   jurisdiction_epoch: int,
     *   source_ids: ?list<string>,
     *   updated_at: string
     * }
     */
    public function broadcastWith(): array
    {
        return [
            'jurisdiction_epoch' => $this->jurisdictionEpoch,
            'source_ids' => $this->sourceIds,
            'updated_at' => now()->toIso8601String(),
        ];
    }
}
