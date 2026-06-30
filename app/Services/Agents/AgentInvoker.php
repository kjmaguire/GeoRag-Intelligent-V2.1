<?php

declare(strict_types=1);

namespace App\Services\Agents;

use App\Services\Audit\AuditEmitter;
use Closure;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Redis;
use InvalidArgumentException;
use RuntimeException;
use Throwable;

/**
 * GeoRAG agent operational contract — Laravel side (Phase 0 step 5.1).
 *
 * Mirrors the @georag_agent decorator on the Python side. Most agents in
 * Phase 4+ are FastAPI-side; this class exists for the smaller set of
 * Laravel-internal agents (Kestra integration adapters, Horizon-dispatched
 * background tasks).
 *
 * Usage:
 *
 *   $result = app(AgentInvoker::class)->invoke(
 *       name: 'Some Laravel Agent',
 *       riskTier: 'R0',
 *       version: '0.1.0',
 *       ctx: new AgentContext(workspaceId: $workspaceId, actorId: $userId),
 *       handler: function (AgentContext $ctx) {
 *           // ... agent body ...
 *           return ['status' => 'ok'];
 *       },
 *   );
 *
 *   if ($result->outcome === 'success') { ... }
 */
class AgentInvoker
{
    private const VALID_RISK_TIERS = ['R0', 'R1', 'R2', 'R3', 'R4', 'R5'];

    /** @var array<string, array{policy: array<string, mixed>, expires_at: float}> */
    private array $timeoutCache = [];

    private const CACHE_TTL_SECONDS = 60;

    public function __construct(private readonly AuditEmitter $auditEmitter) {}

    /**
     * Invoke an agent under the operational contract.
     *
     * @param Closure(AgentContext): mixed $handler
     */
    public function invoke(
        string $name,
        string $riskTier,
        string $version,
        AgentContext $ctx,
        Closure $handler,
    ): AgentResult {
        if (! in_array($riskTier, self::VALID_RISK_TIERS, true)) {
            throw new InvalidArgumentException("invalid riskTier: {$riskTier}");
        }

        $ctx->agentName = $name;
        $ctx->agentVersion = $version;
        $ctx->riskTier = $riskTier;

        $policy = $this->loadTimeoutPolicy($name);
        $this->circuitCheck($name, $ctx->workspaceId, $policy);

        // Idempotency check (R2+).
        $idHash = null;
        $idComponents = null;
        if (! $ctx->bypassIdempotency) {
            [$idHash, $idComponents] = $this->computeIdempotencyKey($ctx) ?? [null, null];
            if ($idHash !== null) {
                if ($cached = $this->idempotencyLookup($idHash)) {
                    return new AgentResult(
                        value: $cached['result_summary'],
                        outcome: 'deduped',
                        ctx: $ctx,
                        durationMs: 0,
                        deduped: true,
                    );
                }
            }
        }

        $start = hrtime(true);
        $outcome = 'success';
        $value = null;
        $error = null;
        try {
            $value = $handler($ctx);
        } catch (Throwable $e) {
            $outcome = 'failure';
            $error = get_class($e).': '.$e->getMessage();
        }
        $durationMs = (int) ((hrtime(true) - $start) / 1_000_000);

        $this->circuitRecord($name, $ctx->workspaceId, $policy, success: $outcome === 'success');

        if ($outcome === 'success' && $idHash !== null) {
            try {
                $this->idempotencyStore(
                    $idHash,
                    $idComponents,
                    $ctx,
                    is_array($value) ? $value : ['value_repr' => mb_substr(var_export($value, true), 0, 200)],
                    $outcome,
                );
            } catch (Throwable) {
                // best-effort
            }
        }

        try {
            $this->auditEmitter->emit(
                actionType: 'agent.invoke.'.$outcome,
                workspaceId: $ctx->workspaceId,
                actorId: $ctx->actorId,
                actorKind: 'agent',
                targetSchema: 'workspace',
                targetTable: 'agent_timeouts',
                targetId: $name,
                payload: [
                    'agent_name' => $name,
                    'agent_version' => $version,
                    'risk_tier' => $riskTier,
                    'invocation_id' => $ctx->invocationId,
                    'duration_ms' => $durationMs,
                    'outcome' => $outcome,
                    'error' => $error,
                ],
                traceId: $ctx->traceId,
            );
        } catch (Throwable) {
            // best-effort
        }

        return new AgentResult(
            value: $value,
            outcome: $outcome,
            ctx: $ctx,
            durationMs: $durationMs,
            deduped: false,
            error: $error,
        );
    }

    /**
     * @return array<string, mixed>
     */
    private function loadTimeoutPolicy(string $name): array
    {
        $now = microtime(true);
        if (isset($this->timeoutCache[$name]) && $this->timeoutCache[$name]['expires_at'] > $now) {
            return $this->timeoutCache[$name]['policy'];
        }

        $row = DB::selectOne(
            'SELECT * FROM workspace.agent_timeouts WHERE agent_name = ?',
            [$name],
        );

        $policy = $row !== null ? (array) $row : [
            'agent_name' => $name,
            'risk_tier' => 'R0',
            'soft_timeout_ms' => 30_000,
            'hard_timeout_ms' => 120_000,
            'retry_count' => 0,
            'circuit_breaker_scope' => 'workspace',
            'failure_threshold' => 5,
            'cool_down_seconds' => 300,
        ];

        $this->timeoutCache[$name] = ['policy' => $policy, 'expires_at' => $now + self::CACHE_TTL_SECONDS];

        return $policy;
    }

    private function circuitKey(string $name, ?string $workspaceId, string $scope): string
    {
        if ($scope === 'global' || $workspaceId === null) {
            return "georag:cb:{$name}:_global";
        }

        return "georag:cb:{$name}:{$workspaceId}";
    }

    /**
     * @param array<string, mixed> $policy
     */
    private function circuitCheck(string $name, ?string $workspaceId, array $policy): void
    {
        if ($policy['circuit_breaker_scope'] === 'none') {
            return;
        }
        $key = $this->circuitKey($name, $workspaceId, $policy['circuit_breaker_scope']);
        $val = Redis::get($key);
        if ($val !== null && (int) $val >= $policy['failure_threshold']) {
            throw new RuntimeException(
                "circuit open for {$name} (failures={$val}, threshold={$policy['failure_threshold']})",
            );
        }
    }

    /**
     * @param array<string, mixed> $policy
     */
    private function circuitRecord(string $name, ?string $workspaceId, array $policy, bool $success): void
    {
        if ($policy['circuit_breaker_scope'] === 'none') {
            return;
        }
        $key = $this->circuitKey($name, $workspaceId, $policy['circuit_breaker_scope']);
        if ($success) {
            Redis::del([$key]);
        } else {
            Redis::incr($key);
            Redis::expire($key, $policy['cool_down_seconds']);
        }
    }

    /**
     * @return array{string, array<string, mixed>}|null [hash_hex, components]
     */
    private function computeIdempotencyKey(AgentContext $ctx): ?array
    {
        if (in_array($ctx->riskTier, ['R0', 'R1'], true)) {
            return null;
        }

        $components = match ($ctx->riskTier) {
            'R2' => [
                'workspace_id' => $ctx->workspaceId,
                'document_id' => $ctx->documentId,
                'agent_name' => $ctx->agentName,
                'agent_version' => $ctx->agentVersion,
            ],
            'R3' => [
                'workspace_id' => $ctx->workspaceId,
                'export_request_id' => $ctx->exportRequestId,
                'agent_name' => $ctx->agentName,
            ],
            'R4' => [
                'workspace_id' => $ctx->workspaceId,
                'sync_target' => $ctx->syncTarget,
                'sync_request_id' => $ctx->syncRequestId,
            ],
            'R5' => [
                'workspace_id' => $ctx->workspaceId,
                'target_id' => $ctx->targetId,
                'signoff_session_id' => $ctx->signoffSessionId,
            ],
            default => throw new InvalidArgumentException("unknown riskTier: {$ctx->riskTier}"),
        };

        foreach ($components as $key => $value) {
            if ($value === null) {
                throw new InvalidArgumentException(
                    "{$ctx->riskTier} idempotency requires non-null '{$key}' on AgentContext",
                );
            }
        }

        ksort($components);
        $serialized = json_encode($components, JSON_THROW_ON_ERROR);
        $hash = hash('sha256', $serialized, binary: false);

        return [$hash, $components];
    }

    /**
     * @return array<string, mixed>|null
     */
    private function idempotencyLookup(string $hashHex): ?array
    {
        $row = DB::selectOne(
            "SELECT id::text AS id, result_summary, outcome, created_at::text AS created_at
             FROM workspace.idempotency_keys WHERE key_hash = decode(?, 'hex')",
            [$hashHex],
        );

        return $row !== null ? (array) $row : null;
    }

    /**
     * @param array<string, mixed> $components
     * @param array<string, mixed> $resultSummary
     */
    private function idempotencyStore(
        string $hashHex,
        array $components,
        AgentContext $ctx,
        array $resultSummary,
        string $outcome,
    ): void {
        DB::statement(
            "INSERT INTO workspace.idempotency_keys
                (key_hash, key_components, risk_tier, workspace_id, agent_name,
                 agent_version, invocation_id, result_summary, outcome)
             VALUES (decode(?, 'hex'), ?::jsonb, ?, ?::uuid, ?, ?, ?::uuid, ?::jsonb, ?)
             ON CONFLICT (key_hash) DO NOTHING",
            [
                $hashHex,
                json_encode($components, JSON_THROW_ON_ERROR),
                $ctx->riskTier,
                $ctx->workspaceId,
                $ctx->agentName,
                $ctx->agentVersion,
                $ctx->invocationId,
                json_encode($resultSummary, JSON_THROW_ON_ERROR),
                $outcome,
            ],
        );
    }
}
