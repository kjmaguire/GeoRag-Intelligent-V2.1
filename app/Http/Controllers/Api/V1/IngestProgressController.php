<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use Carbon\CarbonInterface;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Symfony\Component\HttpFoundation\Response;

/**
 * GET /api/ingest-progress/{run_id} — per-run polling endpoint (Phase 4 of
 * the reliability spec).
 *
 * Reads a single silver.ingest_progress row scoped to the authenticated
 * user's projects. Returns 404 when the run doesn't exist OR when it
 * belongs to a project the user can't access — never 403, to prevent
 * cross-workspace existence fingerprinting (spec T11).
 *
 * Used by IngestionRuns.tsx as the safety net when Reverb is down or a
 * terminal event was missed.
 */
class IngestProgressController extends Controller
{
    public function show(Request $request, string $runId): JsonResponse
    {
        if (preg_match('/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i', $runId) !== 1) {
            return $this->notFound();
        }

        $row = DB::table('silver.ingest_progress')
            ->where('run_id', $runId)
            ->select([
                'run_id',
                'project_id',
                'workspace_id',
                'minio_key',
                'filename',
                'status',
                'current_stage',
                'current_step',
                'step_index',
                'total_steps',
                'started_at',
                'last_stage_started_at',
                'last_heartbeat_at',
                'completed_at',
                'failed_at',
                'error_text',
                'attempt_number',
                'parent_run_id',
                'triggered_by',
                'report_id',
            ])
            ->first();

        if ($row === null) {
            return $this->notFound();
        }

        // Workspace scoping — does this user actually have access to the
        // run's project? If not, return 404 (not 403) so a probe can't
        // confirm whether a run_id exists in another workspace.
        $hasAccess = $request->user()
            ?->projects()
            ?->where('silver.projects.project_id', $row->project_id)
            ?->exists();

        if (! $hasAccess) {
            return $this->notFound();
        }

        return response()->json([
            'run_id' => $row->run_id,
            'project_id' => $row->project_id,
            'minio_key' => $row->minio_key,
            'filename' => $row->filename,
            'status' => $row->status,
            'current_stage' => $row->current_stage,
            'current_step' => $row->current_step,
            'step_index' => (int) $row->step_index,
            'total_steps' => (int) $row->total_steps,
            'attempt_number' => (int) $row->attempt_number,
            'parent_run_id' => $row->parent_run_id,
            'triggered_by' => $row->triggered_by,
            'started_at' => $this->iso($row->started_at),
            'last_stage_started_at' => $this->iso($row->last_stage_started_at),
            'last_heartbeat_at' => $this->iso($row->last_heartbeat_at),
            'completed_at' => $this->iso($row->completed_at),
            'failed_at' => $this->iso($row->failed_at),
            'error' => $row->error_text,
            'report_id' => $row->report_id,
        ]);
    }

    private function notFound(): JsonResponse
    {
        return response()->json(
            ['error' => 'not_found'],
            Response::HTTP_NOT_FOUND,
        );
    }

    private function iso(mixed $ts): ?string
    {
        if ($ts === null || $ts === '') {
            return null;
        }
        if ($ts instanceof CarbonInterface) {
            return $ts->toIso8601String();
        }

        return (string) $ts;
    }
}
