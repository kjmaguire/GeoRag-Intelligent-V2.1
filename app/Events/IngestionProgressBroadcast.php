<?php

declare(strict_types=1);

namespace App\Events;

use App\Http\Controllers\Internal\IngestionProgressBroadcastController;
use Illuminate\Broadcasting\InteractsWithSockets;
use Illuminate\Broadcasting\PrivateChannel;
use Illuminate\Contracts\Broadcasting\ShouldBroadcastNow;
use Illuminate\Foundation\Events\Dispatchable;
use Illuminate\Queue\SerializesModels;

/**
 * Real-time ingestion progress event — Phase 1 of the reliability spec.
 *
 * Dispatched from {@see IngestionProgressBroadcastController}
 * after the FastAPI side persists the conditional terminal-state row in
 * silver.ingest_progress. The IngestionRuns UI subscribes on the existing
 * `project.{projectId}.ingestion` private channel and flips the row
 * immediately, instead of waiting for its next snapshot poll.
 *
 * Statuses emitted:
 *   - 'started' / progress updates (stage = preflight | parse | persist | …)
 *   - 'completed' (terminal — fires from embed_verify when all chunks land)
 *   - 'failed' / 'timed_out' / 'cancelled' (terminal — fires from
 *      on_failure_task or stale_run_detector)
 *
 * The durable record is the DB row. This broadcast is the latency
 * optimisation — the polling fallback (Phase 4) still catches anything
 * that doesn't reach the WebSocket.
 */
class IngestionProgressBroadcast implements ShouldBroadcastNow
{
    use Dispatchable;
    use InteractsWithSockets;
    use SerializesModels;

    public function __construct(
        public readonly string $workspaceId,
        public readonly string $projectId,
        public readonly string $pipelineRunId,
        public readonly string $stage,
        public readonly string $status,
        public readonly ?string $message = null,
        public readonly ?int $pct = null,
    ) {}

    public function broadcastOn(): array
    {
        return [
            new PrivateChannel('project.'.$this->projectId.'.ingestion'),
        ];
    }

    public function broadcastAs(): string
    {
        return 'ingestion.progress';
    }

    /**
     * @return array{
     *   workspace_id: string,
     *   project_id: string,
     *   pipeline_run_id: string,
     *   stage: string,
     *   status: string,
     *   message: ?string,
     *   pct: ?int,
     *   timestamp: string
     * }
     */
    public function broadcastWith(): array
    {
        return [
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->projectId,
            'pipeline_run_id' => $this->pipelineRunId,
            'stage' => $this->stage,
            'status' => $this->status,
            'message' => $this->message,
            'pct' => $this->pct,
            'timestamp' => now()->toIso8601String(),
        ];
    }
}
