<?php

declare(strict_types=1);

namespace App\Events\Workspace;

use Illuminate\Broadcasting\InteractsWithSockets;
use Illuminate\Broadcasting\PrivateChannel;
use Illuminate\Contracts\Broadcasting\ShouldBroadcastNow;
use Illuminate\Foundation\Events\Dispatchable;
use Illuminate\Queue\SerializesModels;

/**
 * Workspace-level activity event — Phase 3 of the real-time staleness fix.
 *
 * Drives the workspace-scoped Foundry pages (Portfolio, Projects) so they
 * re-fetch when ANY project inside the workspace gets new data — without
 * each page needing to subscribe to every project's per-project channel.
 *
 * Channel: private-workspace.{workspaceId}.activity
 *
 * This channel was registered in routes/channels.php as part of the
 * dashboard spec §6 work but never used by a writer. Phase 3 wires it up.
 *
 * Distinct from {@see App\Events\WorkspaceDataUpdated}:
 *   - WorkspaceDataUpdated fires on a single project's channel after the
 *     debounced MV refresh succeeds (project-scoped).
 *   - WorkspaceActivityBroadcast fires on the workspace's channel
 *     whenever anything workspace-wide changes — new project, project
 *     deleted, KPI rollup refreshed, etc.
 *
 * Pages call router.reload({ only: affectedProps }) when they receive an
 * event whose affected_types intersect with their interest. The hook
 * (useWorkspaceActivity) applies a 2-second trailing debounce so a burst
 * of per-project completions collapses into one reload.
 */
class WorkspaceActivityBroadcast implements ShouldBroadcastNow
{
    use Dispatchable;
    use InteractsWithSockets;
    use SerializesModels;

    /**
     * @param list<string> $affectedTypes Keys for the receiver to filter on (e.g. 'projects', 'kpis', 'activity', 'cost', 'tickets', 'traces').
     * @param array<string, mixed> $payload Optional richer context — small, free-form. Receivers use this as a hint, not a data transport.
     */
    public function __construct(
        public readonly string $workspaceId,
        public readonly array $affectedTypes,
        public readonly array $payload = [],
    ) {}

    public function broadcastOn(): array
    {
        return [
            new PrivateChannel('workspace.'.$this->workspaceId.'.activity'),
        ];
    }

    public function broadcastAs(): string
    {
        return 'workspace.activity';
    }

    /**
     * @return array{
     *   workspace_id: string,
     *   affected_types: list<string>,
     *   payload: array<string, mixed>,
     *   updated_at: string
     * }
     */
    public function broadcastWith(): array
    {
        return [
            'workspace_id' => $this->workspaceId,
            'affected_types' => $this->affectedTypes,
            'payload' => $this->payload,
            'updated_at' => now()->toIso8601String(),
        ];
    }
}
