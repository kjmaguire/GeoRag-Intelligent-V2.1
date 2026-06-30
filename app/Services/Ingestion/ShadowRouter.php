<?php

declare(strict_types=1);

namespace App\Services\Ingestion;

use App\Services\Audit\AuditEmitter;
use App\Services\FastApiJwtMinter;
use Illuminate\Contracts\Config\Repository as ConfigRepository;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Str;
use InvalidArgumentException;
use RuntimeException;
use Throwable;

/**
 * GeoRAG Phase 1 Step 5 — ShadowRouter for ``ingest_pdf``.
 *
 * Decides per-request whether to dual-write a PDF ingestion to BOTH the
 * v1.49 path (Dagster-driven, auto-triggered by the bronze upload sensor)
 * AND the new Hatchet ``ingest_pdf`` workflow. Inserts an initial
 * ``silver.shadow_runs`` row with ``classification='partial'`` so the
 * ``ai:shadow_diff`` worker (Step 5B) can pair both sides when they
 * complete.
 *
 * Decision inputs (in priority order):
 *   1. Workspace-scoped flag in ``workspace.feature_flags`` for this
 *      workspace_id+ingest_pdf_hatchet_traffic_pct.
 *   2. Platform default (workspace_id IS NULL).
 *   3. ``ingest_pdf_shadow_enabled`` master switch (also a feature flag).
 *
 * The Hatchet trigger goes through FastAPI's ``/internal/v1/shadow/ingest_pdf/trigger``
 * route — the Hatchet client lives there, not on the PHP side. FastAPI
 * accepts our X-Service-Key (FastApiJwtMinter for the Bearer JWT, plus
 * the long-lived FASTAPI_SERVICE_KEY in the X-Service-Key header).
 */
class ShadowRouter
{
    private const FLAG_TRAFFIC_PCT = 'ingest_pdf_hatchet_traffic_pct';

    private const FLAG_ENABLED = 'ingest_pdf_shadow_enabled';

    public function __construct(
        private readonly AuditEmitter $auditEmitter,
        private readonly FastApiJwtMinter $jwtMinter,
        private readonly ConfigRepository $config,
    ) {}

    /**
     * Decide whether to dual-write this upload, and if so, fire the
     * Hatchet workflow and record the partial shadow_runs row.
     *
     * Returns:
     *   - kind='single':     no dual-write happened (either flag off, % roll
     *                        missed, or workspace_id missing). The caller
     *                        proceeds with the v1.49-only path.
     *   - kind='dual_write': shadow row inserted + Hatchet triggered.
     *
     * @return array{
     *     kind: 'single'|'dual_write',
     *     correlation_token: ?string,
     *     shadow_runs_id: ?string,
     *     hatchet_workflow_run_id: ?string,
     *     traffic_pct: int,
     *     reason: ?string,
     * }
     */
    public function maybeShadow(
        string $workspaceId,
        string $minioKey,
        int $fileSize,
        ?string $projectId = null,
        ?int $vendorProfileId = null,
        ?int $actorId = null,
    ): array {
        if ($workspaceId === '') {
            throw new InvalidArgumentException('workspaceId is required');
        }
        if ($minioKey === '') {
            throw new InvalidArgumentException('minioKey is required');
        }

        $enabled = $this->resolveBoolFlag(self::FLAG_ENABLED, $workspaceId, default: true);
        if (! $enabled) {
            return $this->singleResult(
                trafficPct: 0,
                reason: 'ingest_pdf_shadow_enabled=false',
            );
        }

        $trafficPct = $this->resolveIntFlag(self::FLAG_TRAFFIC_PCT, $workspaceId, default: 0);
        if ($trafficPct <= 0) {
            return $this->singleResult(trafficPct: $trafficPct, reason: 'traffic_pct=0');
        }

        // Hash the workspace_id + minio_key so the dual-write decision is
        // deterministic per upload — same input always lands the same side
        // of the threshold, so reruns are reproducible.
        $roll = hexdec(substr(hash('sha256', $workspaceId.$minioKey), 0, 4)) % 100;
        if ($roll >= $trafficPct) {
            return $this->singleResult(
                trafficPct: $trafficPct,
                reason: "roll={$roll} >= {$trafficPct}",
            );
        }

        $correlationToken = 'phase1-shadow-'.Str::uuid()->toString();
        $projectId = $projectId ?: $this->extractProjectIdFromKey($minioKey);

        // Insert the initial shadow_runs row (classification='partial' until
        // the diff worker pairs both sides).
        $shadowId = $this->insertShadowRow(
            workspaceId: $workspaceId,
            correlationToken: $correlationToken,
            minioKey: $minioKey,
        );

        // Trigger Hatchet via FastAPI.
        $workflowRunId = null;
        try {
            $workflowRunId = $this->triggerHatchet(
                workspaceId: $workspaceId,
                projectId: $projectId,
                minioKey: $minioKey,
                fileSize: $fileSize,
                vendorProfileId: $vendorProfileId,
                correlationToken: $correlationToken,
                actorId: $actorId,
            );
        } catch (Throwable $e) {
            // Hatchet trigger failed — record on the row so the diff worker
            // marks this run 'fatal'. The Laravel-side upload still proceeds;
            // the v1.49 path is unaffected.
            Log::warning('ShadowRouter: Hatchet trigger failed', [
                'workspace_id' => $workspaceId,
                'minio_key' => $minioKey,
                'correlation_token' => $correlationToken,
                'error' => $e->getMessage(),
            ]);
            DB::statement(
                'UPDATE silver.shadow_runs SET error_hatchet = ?, completed_at = now() WHERE id = ?::uuid',
                [substr($e->getMessage(), 0, 1000), $shadowId],
            );
        }

        // Audit: record the shadow decision.
        try {
            $this->auditEmitter->emit(
                actionType: 'ingest_pdf.shadow.dispatched',
                workspaceId: $workspaceId,
                actorId: $actorId,
                actorKind: AuditEmitter::ACTOR_USER,
                targetSchema: 'silver',
                targetTable: 'shadow_runs',
                targetId: $shadowId,
                payload: [
                    'correlation_token' => $correlationToken,
                    'minio_key' => $minioKey,
                    'project_id' => $projectId,
                    'traffic_pct' => $trafficPct,
                    'roll' => $roll,
                    'hatchet_workflow_run_id' => $workflowRunId,
                ],
            );
        } catch (Throwable $e) {
            Log::warning('ShadowRouter: audit emit failed', ['error' => $e->getMessage()]);
        }

        return [
            'kind' => 'dual_write',
            'correlation_token' => $correlationToken,
            'shadow_runs_id' => $shadowId,
            'hatchet_workflow_run_id' => $workflowRunId,
            'traffic_pct' => $trafficPct,
            'reason' => null,
        ];
    }

    private function resolveBoolFlag(string $flag, string $workspaceId, bool $default): bool
    {
        $row = DB::selectOne(
            'SELECT bool_value FROM workspace.feature_flags
              WHERE flag_name = ?
                AND (workspace_id = ?::uuid OR workspace_id IS NULL)
              ORDER BY workspace_id NULLS LAST LIMIT 1',
            [$flag, $workspaceId],
        );

        if ($row === null) {
            return $default;
        }

        return (bool) ($row->bool_value ?? $default);
    }

    private function resolveIntFlag(string $flag, string $workspaceId, int $default): int
    {
        $row = DB::selectOne(
            'SELECT int_value FROM workspace.feature_flags
              WHERE flag_name = ?
                AND (workspace_id = ?::uuid OR workspace_id IS NULL)
              ORDER BY workspace_id NULLS LAST LIMIT 1',
            [$flag, $workspaceId],
        );

        if ($row === null) {
            return $default;
        }

        $val = $row->int_value;

        return $val !== null ? max(0, min(100, (int) $val)) : $default;
    }

    private function insertShadowRow(
        string $workspaceId,
        string $correlationToken,
        string $minioKey,
    ): string {
        // shadow_runs is RLS-enabled; set the workspace GUC inside the
        // INSERT transaction so the WITH CHECK clause passes.
        return DB::transaction(function () use ($workspaceId, $correlationToken, $minioKey) {
            DB::statement("SELECT set_config('app.workspace_id', ?, true)", [$workspaceId]);
            $row = DB::selectOne(
                "INSERT INTO silver.shadow_runs
                    (workspace_id, workflow_kind, correlation_token, minio_key,
                     classification, started_at)
                 VALUES (?::uuid, 'ingest_pdf', ?, ?, 'partial', clock_timestamp())
                 RETURNING id::text AS id",
                [$workspaceId, $correlationToken, $minioKey],
            );

            return $row->id;
        });
    }

    private function triggerHatchet(
        string $workspaceId,
        ?string $projectId,
        string $minioKey,
        int $fileSize,
        ?int $vendorProfileId,
        string $correlationToken,
        ?int $actorId,
    ): string {
        $url = rtrim(
            $this->config->get('services.fastapi.url') ?: env('FASTAPI_INTERNAL_URL', 'http://fastapi:8000'),
            '/',
        ).'/internal/v1/shadow/ingest_pdf/trigger';

        $serviceKey = env('FASTAPI_SERVICE_KEY');
        if (! $serviceKey) {
            throw new RuntimeException('FASTAPI_SERVICE_KEY not configured');
        }

        // FastApiJwtMinter::mint(int|string $userId, string $projectId, array $roles = [])
        $jwt = $this->jwtMinter->mint(
            userId: $actorId ?? 0,
            projectId: $projectId ?? 'phase1-shadow',
            roles: ['shadow:trigger'],
        );

        $resp = Http::withHeaders([
            'Authorization' => "Bearer {$jwt}",
            'X-Service-Key' => $serviceKey,
        ])->timeout(15)->post($url, [
            'workspace_id' => $workspaceId,
            'project_id' => $projectId ?: 'unknown',
            'minio_key' => $minioKey,
            'file_size' => $fileSize,
            'vendor_profile_id' => $vendorProfileId,
            'correlation_token' => $correlationToken,
            'actor_id' => $actorId,
        ]);

        if (! $resp->successful()) {
            throw new RuntimeException(
                'Hatchet trigger HTTP '.$resp->status().': '.substr($resp->body(), 0, 200),
            );
        }

        $body = $resp->json();
        $runId = $body['workflow_run_id'] ?? null;
        if (! is_string($runId) || $runId === '') {
            throw new RuntimeException('Hatchet trigger response missing workflow_run_id');
        }

        return $runId;
    }

    private function extractProjectIdFromKey(string $minioKey): string
    {
        // Bronze keys are reports/{projectId}/{timestamp}_{filename}.pdf
        $parts = explode('/', $minioKey);
        if (count($parts) >= 2 && $parts[0] === 'reports') {
            return $parts[1];
        }

        return 'unknown';
    }

    /**
     * @return array{kind:'single', correlation_token:null, shadow_runs_id:null, hatchet_workflow_run_id:null, traffic_pct:int, reason:string}
     */
    private function singleResult(int $trafficPct, string $reason): array
    {
        return [
            'kind' => 'single',
            'correlation_token' => null,
            'shadow_runs_id' => null,
            'hatchet_workflow_run_id' => null,
            'traffic_pct' => $trafficPct,
            'reason' => $reason,
        ];
    }
}
