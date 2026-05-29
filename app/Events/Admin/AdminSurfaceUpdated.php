<?php

declare(strict_types=1);

namespace App\Events\Admin;

use Illuminate\Broadcasting\InteractsWithSockets;
use Illuminate\Broadcasting\PrivateChannel;
use Illuminate\Contracts\Broadcasting\ShouldBroadcastNow;
use Illuminate\Foundation\Events\Dispatchable;
use Illuminate\Queue\SerializesModels;

/**
 * Generic admin surface push — Phase 2 of the real-time staleness fix.
 *
 * One event class drives the 10 admin pages that needed live updates. The
 * surface discriminator picks the channel; affected_props feeds the
 * receiving page's router.reload({ only: [...] }) call.
 *
 * Channel naming (matches the precedent in routes/channels.php):
 *   - 'admin.<surface>'                  → list-page channels (no surface_id)
 *       e.g. admin.workflow-runs, admin.reports, admin.ml-training
 *   - 'admin.<surface>.<surface_id>'     → per-resource drilldown channels
 *       e.g. admin.target-run.{run_id}
 *
 * Pages subscribe via the useAdminSurfaceUpdated React hook (mirrors
 * useWorkspaceDataUpdated from Phase 1, but lives on the admin Gate).
 *
 * Distinct from the two pre-existing admin events:
 *   - ReportBuildProgress (per-build cockpit progress, admin.reports.{build_id})
 *     — domain-specific stage labels are useful in the payload, kept as-is.
 *   - IngestionReviewDispositionChanged (multi-operator disposition sync on
 *     admin.ingestion-review) — kept as-is; new "queue grew" signals layer on
 *     by also dispatching AdminSurfaceUpdated on the same channel. Multiple
 *     event names coexist fine on one channel.
 */
class AdminSurfaceUpdated implements ShouldBroadcastNow
{
    use Dispatchable;
    use InteractsWithSockets;
    use SerializesModels;

    /**
     * @param list<string> $affectedProps Keys the receiving page reloads via router.reload({ only: [...] }).
     * @param array<string, mixed> $payload Optional richer info (status, kind, run_id, count, etc.). Best-effort context for the receiver; the durable record is the underlying DB row.
     */
    public function __construct(
        public readonly string $surface,
        public readonly ?string $surfaceId,
        public readonly array $affectedProps,
        public readonly array $payload = [],
    ) {}

    public function broadcastOn(): array
    {
        $channel = 'admin.'.$this->surface
            .($this->surfaceId !== null ? '.'.$this->surfaceId : '');

        return [new PrivateChannel($channel)];
    }

    public function broadcastAs(): string
    {
        return 'admin.surface_updated';
    }

    /**
     * @return array{
     *   surface: string,
     *   surface_id: ?string,
     *   affected_props: list<string>,
     *   payload: array<string, mixed>,
     *   timestamp: string
     * }
     */
    public function broadcastWith(): array
    {
        return [
            'surface' => $this->surface,
            'surface_id' => $this->surfaceId,
            'affected_props' => $this->affectedProps,
            'payload' => $this->payload,
            'timestamp' => now()->toIso8601String(),
        ];
    }
}
