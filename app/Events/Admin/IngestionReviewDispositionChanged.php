<?php

declare(strict_types=1);

namespace App\Events\Admin;

use Illuminate\Broadcasting\InteractsWithSockets;
use Illuminate\Broadcasting\PrivateChannel;
use Illuminate\Contracts\Broadcasting\ShouldBroadcastNow;
use Illuminate\Foundation\Events\Dispatchable;
use Illuminate\Queue\SerializesModels;

/**
 * Broadcasts when an operator changes a review item's disposition in
 * the Silver Review queue (master-plan §3 Step 8f, doc-phase 64).
 *
 * Channel: private-admin.ingestion-review
 * Listeners: any admin currently viewing /admin/ingestion-review
 * receives the event and patches their queue row's status in place
 * without a full page reload.
 *
 * Multi-operator workflow value: prevents two admins from both
 * resolving the same review item independently. The event arrives
 * within ms of the PATCH commit.
 */
class IngestionReviewDispositionChanged implements ShouldBroadcastNow
{
    use Dispatchable, InteractsWithSockets, SerializesModels;

    public function __construct(
        public readonly string $reviewItemId,
        public readonly string $reportId,
        public readonly int $page,
        public readonly string $newStatus,
        public readonly ?string $reason,
        public readonly ?int $actorId,
        public readonly bool $reOcrTriggered = false,
    ) {}

    public function broadcastOn(): array
    {
        return [
            new PrivateChannel('admin.ingestion-review'),
        ];
    }

    public function broadcastAs(): string
    {
        return 'IngestionReviewDispositionChanged';
    }

    public function broadcastWith(): array
    {
        return [
            'review_item_id' => $this->reviewItemId,
            'report_id' => $this->reportId,
            'page' => $this->page,
            'new_status' => $this->newStatus,
            'reason' => $this->reason,
            'actor_id' => $this->actorId,
            're_ocr_triggered' => $this->reOcrTriggered,
            'timestamp' => now()->toIso8601String(),
        ];
    }
}
