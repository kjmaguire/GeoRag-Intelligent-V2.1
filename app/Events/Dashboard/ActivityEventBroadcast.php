<?php

declare(strict_types=1);

namespace App\Events\Dashboard;

use Illuminate\Broadcasting\InteractsWithSockets;
use Illuminate\Broadcasting\PrivateChannel;
use Illuminate\Contracts\Broadcasting\ShouldBroadcastNow;
use Illuminate\Foundation\Events\Dispatchable;
use Illuminate\Queue\SerializesModels;

/**
 * Broadcasts a new activity event to the workspace dashboard activity feed.
 *
 * Channel: private-workspace.{workspaceId}.activity
 * Per dashboard spec §6 — pushed in real-time, no queuing.
 */
class ActivityEventBroadcast implements ShouldBroadcastNow
{
    use Dispatchable;
    use InteractsWithSockets;
    use SerializesModels;

    public function __construct(
        private readonly string $workspaceId,
        public readonly array $payload,
    ) {}

    public function broadcastOn(): array
    {
        return [
            new PrivateChannel("workspace.{$this->workspaceId}.activity"),
        ];
    }

    public function broadcastAs(): string
    {
        return 'ActivityEventBroadcast';
    }

    public function broadcastWith(): array
    {
        return $this->payload;
    }
}
