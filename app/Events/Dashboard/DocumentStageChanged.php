<?php

declare(strict_types=1);

namespace App\Events\Dashboard;

use Illuminate\Broadcasting\InteractsWithSockets;
use Illuminate\Broadcasting\PrivateChannel;
use Illuminate\Contracts\Broadcasting\ShouldBroadcastNow;
use Illuminate\Foundation\Events\Dispatchable;
use Illuminate\Queue\SerializesModels;

/**
 * Broadcasts when a document transitions between medallion stages.
 *
 * Channel: private-project.{projectId}.ingestion
 * Per dashboard spec §6 — updates ingestion health bars and document inventory.
 */
class DocumentStageChanged implements ShouldBroadcastNow
{
    use Dispatchable;
    use InteractsWithSockets;
    use SerializesModels;

    public function __construct(
        private readonly string $projectId,
        public readonly string $documentId,
        public readonly string $oldStage,
        public readonly string $newStage,
    ) {}

    public function broadcastOn(): array
    {
        return [
            new PrivateChannel("project.{$this->projectId}.ingestion"),
        ];
    }

    public function broadcastAs(): string
    {
        return 'DocumentStageChanged';
    }

    public function broadcastWith(): array
    {
        return [
            'document_id' => $this->documentId,
            'old_stage' => $this->oldStage,
            'new_stage' => $this->newStage,
            'timestamp' => now()->toIso8601String(),
        ];
    }
}
