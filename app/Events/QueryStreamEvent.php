<?php

declare(strict_types=1);

namespace App\Events;

use Illuminate\Broadcasting\InteractsWithSockets;
use Illuminate\Broadcasting\PrivateChannel;
use Illuminate\Contracts\Broadcasting\ShouldBroadcastNow;
use Illuminate\Foundation\Events\Dispatchable;
use Illuminate\Queue\SerializesModels;

/**
 * Broadcast a single SSE token/event from the FastAPI RAG stream to the
 * React frontend via Reverb.
 *
 * Uses ShouldBroadcastNow so the event is pushed to the WebSocket server
 * inline in the Horizon job rather than being queued a second time.
 *
 * Channel name: "query.{queryId}" (private — requires Sanctum auth).
 * React subscribes via: Echo.private('query.{queryId}').listen('QueryStreamEvent', ...)
 */
class QueryStreamEvent implements ShouldBroadcastNow
{
    use Dispatchable, InteractsWithSockets, SerializesModels;

    public function __construct(
        private readonly string $channelName,
        public readonly string  $eventType,
        public readonly array   $payload,
    ) {}

    public function broadcastOn(): array
    {
        // Private channel — subscription is gated by routes/channels.php, which
        // verifies the subscriber owns the query_id and still has access to the
        // row's project_id. Prevents any cross-tenant data leak.
        return [
            new PrivateChannel($this->channelName),
        ];
    }

    /**
     * The event name emitted on the channel.
     * Clients listen for 'QueryStreamEvent' regardless of delta/done/error sub-type;
     * the sub-type is carried in payload['event'].
     */
    public function broadcastAs(): string
    {
        return 'QueryStreamEvent';
    }

    public function broadcastWith(): array
    {
        return $this->payload;
    }
}
