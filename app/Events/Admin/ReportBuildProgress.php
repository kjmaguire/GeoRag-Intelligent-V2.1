<?php

declare(strict_types=1);

namespace App\Events\Admin;

use Illuminate\Broadcasting\InteractsWithSockets;
use Illuminate\Broadcasting\PrivateChannel;
use Illuminate\Contracts\Broadcasting\ShouldBroadcastNow;
use Illuminate\Foundation\Events\Dispatchable;
use Illuminate\Queue\SerializesModels;

/**
 * Real-time progress event for §15 generate_report builds.
 *
 * Channel: private-admin.reports.{build_id}
 *
 * Stages emitted by the FastAPI side (via internal POST to
 * /internal/admin/reports/{build_id}/progress, service-key guarded):
 *   - planning       — sections planned, total count known
 *   - section.start  — section drafting started
 *   - section.done   — section drafting completed (body length attached)
 *   - export.gates   — §29 export compliance gates running
 *   - export.done    — artifact uri set ready
 *   - failed         — terminal error with reason
 *
 * The cockpit subscribes on /admin/reports/{build_id} and patches the
 * status row without polling.
 */
class ReportBuildProgress implements ShouldBroadcastNow
{
    use Dispatchable;
    use InteractsWithSockets;
    use SerializesModels;

    public function __construct(
        public readonly string $buildId,
        public readonly string $stage,
        public readonly ?string $sectionId = null,
        public readonly ?string $message = null,
        public readonly ?int $sectionsCompleted = null,
        public readonly ?int $sectionsTotal = null,
    ) {}

    public function broadcastOn(): array
    {
        return [
            new PrivateChannel('admin.reports.'.$this->buildId),
        ];
    }

    public function broadcastAs(): string
    {
        return 'ReportBuildProgress';
    }

    public function broadcastWith(): array
    {
        return [
            'build_id' => $this->buildId,
            'stage' => $this->stage,
            'section_id' => $this->sectionId,
            'message' => $this->message,
            'sections_completed' => $this->sectionsCompleted,
            'sections_total' => $this->sectionsTotal,
            'timestamp' => now()->toIso8601String(),
        ];
    }
}
