<?php

declare(strict_types=1);

namespace App\Events;

use Illuminate\Broadcasting\InteractsWithSockets;
use Illuminate\Broadcasting\PrivateChannel;
use Illuminate\Contracts\Broadcasting\ShouldBroadcastNow;
use Illuminate\Foundation\Events\Dispatchable;
use Illuminate\Queue\SerializesModels;

/**
 * Phase 2b of the reliability spec — "data is ready to query" signal.
 *
 * Distinct from {@see IngestionProgressBroadcast}. That one fires on
 * every per-run state transition (started, completed, failed, etc.) and
 * drives the IngestionRuns UI. This one fires ONCE per workspace after
 * BOTH:
 *
 *   1. The terminal completion broadcast bumped data_version, AND
 *   2. The debounced MV refresh succeeded (gold.mv_refresh_log has a
 *      'completed' row newer than the run's completed_at).
 *
 * Subscribed to by Overview / Lakehouse / DrillholeDetail / Map via the
 * shared useWorkspaceDataUpdated hook. Pages call Inertia partial
 * reloads scoped to the types in `affected_types`.
 *
 * Channel: project.{projectId}.ingestion — reuses the existing private
 * channel so we don't need new broadcasting auth.
 *
 * Event name: 'workspace.data_updated' (the dot prefix in the listener
 * call indicates "raw event name" to Laravel Echo).
 */
class WorkspaceDataUpdated implements ShouldBroadcastNow
{
    use Dispatchable;
    use InteractsWithSockets;
    use SerializesModels;

    /**
     * @param list<string> $affectedTypes high-level data types touched
     *                                    by this update — keys the
     *                                    partial-reload props on the
     *                                    receiving page (e.g.
     *                                    'reports', 'collars',
     *                                    'assays').
     * @param ?int $dataVersion post-bump silver.projects.data_version.
     *                          Phase 4 — drives the MapView Silver MVT
     *                          tile cache-bust (?v={n} query suffix).
     *                          Null when the event source doesn't know
     *                          the version (e.g. non-ingestion writers
     *                          that piggy-back on this event); the
     *                          MapLibre subscriber treats null as "no
     *                          new version info, don't touch tiles".
     */
    public function __construct(
        public readonly string $workspaceId,
        public readonly string $projectId,
        public readonly string $pipelineRunId,
        public readonly array $affectedTypes,
        public readonly ?int $dataVersion = null,
    ) {}

    public function broadcastOn(): array
    {
        return [
            new PrivateChannel('project.'.$this->projectId.'.ingestion'),
        ];
    }

    public function broadcastAs(): string
    {
        return 'workspace.data_updated';
    }

    /**
     * @return array{
     *   workspace_id: string,
     *   project_id: string,
     *   pipeline_run_id: string,
     *   affected_types: list<string>,
     *   data_version: ?int,
     *   updated_at: string
     * }
     */
    public function broadcastWith(): array
    {
        return [
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->projectId,
            'pipeline_run_id' => $this->pipelineRunId,
            'affected_types' => $this->affectedTypes,
            'data_version' => $this->dataVersion,
            'updated_at' => now()->toIso8601String(),
        ];
    }
}
