<?php

declare(strict_types=1);

namespace App\Services\Ingestion;

use Illuminate\Contracts\Redis\Factory as RedisFactory;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;

/**
 * Bump silver.workspaces.data_version + silver.projects.data_version on a
 * confirmed terminal ingestion completion (Phase 2 of the reliability spec).
 *
 * The monotonic trigger on both columns prevents decrements; we only ever
 * increment. Cache readers (answer_runs, tile proxy ETags, etc.) compare
 * the at-query data_version against the current value and miss/refresh
 * when stale, so this bump is the cache-invalidation signal.
 *
 * Idempotency: keyed on `pipeline_run_id` via Redis SETNX (1h TTL). A
 * Hatchet retry of the broadcast post can't double-bump because the
 * second call finds the lock and short-circuits.
 *
 * Never fires from `failed`/`cancelled`/`timed_out` runs — only from
 * `completed`. The caller is responsible for that gate; this service
 * trusts its inputs.
 *
 * Octane-safe: no static state, no per-request leaks.
 */
class WorkspaceDataVersionBumper
{
    /**
     * Redis SETNX guard TTL. 1 hour is long enough that any legitimate
     * retry of the same run's broadcast lands within the window, but
     * short enough that stale keys clean themselves up.
     */
    private const IDEMPOTENCY_TTL_SECONDS = 3600;

    public function __construct(
        private readonly RedisFactory $redis,
    ) {}

    /**
     * Bump data_version on the workspace + project for a completed run.
     *
     * @return array{
     *   bumped: bool,
     *   reason: string,
     *   idempotency_enforced: bool,
     *   workspace_version: int|null,
     *   project_version: int|null
     * }
     */
    public function bump(
        string $workspaceId,
        string $projectId,
        string $pipelineRunId,
    ): array {
        $key = "ingest:data_version_bumped:{$pipelineRunId}";

        // SETNX so a Hatchet retry of the same terminal-state broadcast
        // can't trigger a second bump. set() with NX + EX is the atomic
        // form supported by every Predis/PhpRedis backend.
        //
        // Redis being unreachable must NOT fail the caller. This call used to
        // sit outside any try/catch, so a `RedisException: Connection refused`
        // propagated out of bump(), out of IngestionProgressBroadcastController
        // and returned 500 to FastAPI's progress callback. The visible symptom
        // is the one that matters: data_version never bumps, so every cache
        // reader keeps serving the pre-ingest value and the UI stops
        // self-updating until someone hard-refreshes.
        //
        // So the guard fails OPEN. What it protects against is a DOUBLE
        // increment, and a double increment is benign — data_version is an
        // opaque monotonic cache token, and invalidating a cache twice costs
        // one extra refresh. Losing the bump entirely is the expensive
        // failure. `idempotency_enforced` records which mode ran so a
        // duplicate is explicable rather than mysterious.
        $idempotencyEnforced = true;
        try {
            $acquired = $this->redis->connection()->set(
                $key, '1', 'EX', self::IDEMPOTENCY_TTL_SECONDS, 'NX',
            );
        } catch (\Throwable $e) {
            $idempotencyEnforced = false;
            $acquired = true;
            Log::warning('data_version.bump.idempotency_unavailable', [
                'workspace_id' => $workspaceId,
                'project_id' => $projectId,
                'pipeline_run_id' => $pipelineRunId,
                'error' => $e->getMessage(),
            ]);
        }

        if (! $acquired) {
            Log::info('data_version.bump.skipped_idempotent', [
                'workspace_id' => $workspaceId,
                'project_id' => $projectId,
                'pipeline_run_id' => $pipelineRunId,
            ]);

            return [
                'bumped' => false,
                'reason' => 'idempotent_skip',
                'idempotency_enforced' => true,
                'workspace_version' => null,
                'project_version' => null,
            ];
        }

        try {
            $newWorkspaceVersion = DB::transaction(function () use ($workspaceId, $projectId, &$newProjectVersion) {
                // 2026-08-14 RLS hardening pass — bind the workspace GUC
                // before writing. silver.workspaces / silver.projects are
                // NOT flipped fail-closed by this pass, but their policy
                // is a live candidate for a future one; this UPDATE
                // already knows $workspaceId, so there is no reason for
                // it to depend on the fail-open escape hatch (silent
                // "0 rows updated" under a future fail-closed policy).
                // SET LOCAL is transaction-scoped, matching
                // SetsWorkspaceRlsContext's convention elsewhere.
                DB::statement("SELECT set_config('app.workspace_id', ?, true)", [$workspaceId]);

                $wsRow = DB::selectOne(
                    'UPDATE silver.workspaces
                     SET data_version = data_version + 1, updated_at = NOW()
                     WHERE workspace_id = ?::uuid
                     RETURNING data_version',
                    [$workspaceId],
                );
                $projRow = DB::selectOne(
                    'UPDATE silver.projects
                     SET data_version = data_version + 1, updated_at = NOW()
                     WHERE project_id = ?::uuid
                     RETURNING data_version',
                    [$projectId],
                );
                $newProjectVersion = $projRow?->data_version;

                return $wsRow?->data_version;
            });

            Log::info('data_version.bump.success', [
                'workspace_id' => $workspaceId,
                'project_id' => $projectId,
                'pipeline_run_id' => $pipelineRunId,
                'workspace_version' => $newWorkspaceVersion,
                'project_version' => $newProjectVersion ?? null,
            ]);

            return [
                'bumped' => true,
                'reason' => 'incremented',
                'idempotency_enforced' => $idempotencyEnforced,
                'workspace_version' => $newWorkspaceVersion !== null ? (int) $newWorkspaceVersion : null,
                'project_version' => isset($newProjectVersion) && $newProjectVersion !== null
                    ? (int) $newProjectVersion : null,
            ];
        } catch (\Throwable $e) {
            // Release the lock so a manual retry can re-attempt. We don't
            // want a failed bump to permanently shadow the workspace's
            // ability to ever refresh.
            //
            // Guarded for the same reason as the acquire above: if Redis is
            // what is down, this del() would throw and replace a reported
            // db_error with an unhandled RedisException — turning a
            // diagnosable 200-with-reason into an opaque 500.
            try {
                $this->redis->connection()->del($key);
            } catch (\Throwable $releaseFailure) {
                Log::warning('data_version.bump.lock_release_failed', [
                    'pipeline_run_id' => $pipelineRunId,
                    'error' => $releaseFailure->getMessage(),
                ]);
            }

            Log::warning('data_version.bump.failed', [
                'workspace_id' => $workspaceId,
                'project_id' => $projectId,
                'pipeline_run_id' => $pipelineRunId,
                'error' => $e->getMessage(),
            ]);

            return [
                'bumped' => false,
                'reason' => 'db_error: '.$e->getMessage(),
                'idempotency_enforced' => $idempotencyEnforced,
                'workspace_version' => null,
                'project_version' => null,
            ];
        }
    }
}
