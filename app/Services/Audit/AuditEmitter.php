<?php

declare(strict_types=1);

namespace App\Services\Audit;

use App\Events\Admin\AdminSurfaceUpdated;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;
use InvalidArgumentException;

/**
 * GeoRAG audit ledger — Laravel emitter (Phase 0 step 4.1).
 *
 * Mirrors src/fastapi/app/audit/__init__.py on the PHP side so Laravel
 * controllers and listeners can write into audit.audit_ledger from inside
 * the same DB transaction as the state-changing write they are auditing.
 *
 * The hash chain is computed by the Postgres BEFORE-INSERT trigger
 * (audit.compute_audit_hash) — this class never computes hashes itself.
 *
 * Typical usage:
 *
 *   DB::transaction(function () use ($workspaceId, $userId, $row) {
 *       $row->save();
 *       app(AuditEmitter::class)->emit(
 *           actionType: 'silver.assay_results.update',
 *           workspaceId: $workspaceId,
 *           actorId: $userId,
 *           targetSchema: 'silver',
 *           targetTable: 'assay_results',
 *           targetId: (string) $row->id,
 *           payload: ['changed_fields' => ['au_ppm', 'ag_ppm']],
 *       );
 *   });
 */
class AuditEmitter
{
    public const ACTOR_USER = 'user';

    public const ACTOR_SYSTEM = 'system';

    public const ACTOR_AGENT = 'agent';

    public const ACTOR_WORKFLOW = 'workflow';

    public const ACTOR_EXTERNAL = 'external';

    private const ALLOWED_ACTOR_KINDS = [
        self::ACTOR_USER,
        self::ACTOR_SYSTEM,
        self::ACTOR_AGENT,
        self::ACTOR_WORKFLOW,
        self::ACTOR_EXTERNAL,
    ];

    /**
     * Insert one row into audit.audit_ledger and return its key fields.
     *
     * @param array<string, mixed>|null $payload JSON-serialisable map.
     *
     * @return array{id: string, hash: string, previous_hash: ?string, created_at: string}
     *
     * @throws InvalidArgumentException
     */
    public function emit(
        string $actionType,
        ?string $workspaceId = null,
        ?int $actorId = null,
        string $actorKind = self::ACTOR_SYSTEM,
        ?string $targetSchema = null,
        ?string $targetTable = null,
        ?string $targetId = null,
        ?array $payload = null,
        ?string $traceId = null,
    ): array {
        if ($actionType === '') {
            throw new InvalidArgumentException('action_type is required');
        }

        if (! in_array($actorKind, self::ALLOWED_ACTOR_KINDS, true)) {
            throw new InvalidArgumentException(
                'actor_kind must be one of: '.implode(', ', self::ALLOWED_ACTOR_KINDS),
            );
        }

        // ksort so the PHP-side payload serialisation is stable and matches
        // the Python emitter's sort_keys=True. The trigger uses jsonb's
        // internal representation either way, so this is belt-and-suspenders
        // for human-readable repro.
        $payloadJson = json_encode(
            $payload === null ? new \stdClass : $this->sortRecursive($payload),
            JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE,
        );

        $row = DB::selectOne(
            <<<'SQL'
                INSERT INTO audit.audit_ledger (
                    workspace_id, actor_id, actor_kind, action_type,
                    target_schema, target_table, target_id,
                    payload, trace_id
                )
                VALUES (?::uuid, ?, ?, ?, ?, ?, ?, ?::jsonb, ?)
                RETURNING
                    id::text AS id,
                    encode(hash, 'hex') AS hash,
                    encode(previous_hash, 'hex') AS previous_hash,
                    created_at::text AS created_at
            SQL,
            [
                $workspaceId,
                $actorId,
                $actorKind,
                $actionType,
                $targetSchema,
                $targetTable,
                $targetId,
                $payloadJson,
                $traceId,
            ],
        );

        $result = [
            'id' => $row->id,
            'hash' => $row->hash,
            'previous_hash' => $row->previous_hash !== '' ? $row->previous_hash : null,
            'created_at' => $row->created_at,
        ];

        // Phase 2 real-time staleness fix — every audit row whose action_type
        // ends in '.alert' (or '.acknowledged' for the ack counter rows) is
        // surfaced live on the Admin/AlertsInbox page. Hooking the emit path
        // here covers every alert writer in one place (cost_burn_watcher,
        // reliability_metrics_publisher, stale_run_detector, vllm_security,
        // plus any future ones) without per-workflow code edits.
        //
        // Best-effort: a broadcasting outage must NEVER fail an audit emit —
        // the durable record is the row we just inserted above. Swallow + log.
        if ($this->isAlertActionType($actionType)) {
            try {
                AdminSurfaceUpdated::dispatch(
                    'alerts-inbox',
                    null,
                    ['items'],
                    [
                        'audit_id' => $result['id'],
                        'action_type' => $actionType,
                        'workspace_id' => $workspaceId,
                        'actor_kind' => $actorKind,
                    ],
                );
            } catch (\Throwable $e) {
                Log::warning('AuditEmitter: alerts-inbox broadcast failed', [
                    'audit_id' => $result['id'],
                    'action_type' => $actionType,
                    'error' => $e->getMessage(),
                ]);
            }
        }

        return $result;
    }

    /**
     * True when the action_type signals an operator-visible alert.
     *
     * `*.alert` is the live notification. `*.acknowledged` is the ack
     * counter row written when an admin clicks Acknowledge — surfacing
     * that to the inbox lets other admins see the ack live (matches the
     * multi-operator UX value of IngestionReviewDispositionChanged).
     */
    private function isAlertActionType(string $actionType): bool
    {
        return str_ends_with($actionType, '.alert')
            || str_ends_with($actionType, '.acknowledged');
    }

    /**
     * Recursively ksort an associative array so json_encode produces stable
     * output regardless of insertion order.
     *
     * @param array<mixed> $arr
     *
     * @return array<mixed>
     */
    private function sortRecursive(array $arr): array
    {
        if (array_is_list($arr)) {
            return array_map(
                fn ($v) => is_array($v) ? $this->sortRecursive($v) : $v,
                $arr,
            );
        }

        ksort($arr);

        return array_map(
            fn ($v) => is_array($v) ? $this->sortRecursive($v) : $v,
            $arr,
        );
    }
}
