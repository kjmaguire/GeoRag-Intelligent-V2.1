<?php

declare(strict_types=1);

namespace App\Services\DecisionIntelligence;

use App\Events\Admin\AdminSurfaceUpdated;
use App\Services\Audit\AuditEmitter;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;
use InvalidArgumentException;

/**
 * §21 / §9.12 Decision Intelligence — Laravel facade (doc-phase 133).
 *
 * Mirrors the Python `app.services.decision_intelligence.record_decision`
 * facade (doc-phase 115) so PHP-side capture sites can record §21.3
 * decisions in the same shape as Python-side sites.
 *
 * Eight §21.3 decision types funnel through this one service; each
 * call atomically:
 *
 *   1. INSERTs into `silver.decision_records`
 *   2. INSERTs evidence links into `silver.decision_evidence_links`
 *   3. INSERTs options into `silver.decision_options`
 *   4. (Optionally) INSERTs an outcome row into `silver.decision_outcomes`
 *   5. Emits an `audit.audit_ledger` row via `App\Services\Audit\AuditEmitter`
 *      and back-fills `audit_ledger_id` + `hash` on the decision_records row
 *
 * All steps run inside a single Laravel DB transaction.
 *
 * Platform-level decisions (workflow_enablement of global feature
 * flags, etc.) use the `platform_ops` sentinel workspace seeded by
 * the doc-phase 133 migration.
 */
class RecordDecision
{
    /** UUID of the platform_ops sentinel workspace (doc-phase 133 migration). */
    public const PLATFORM_OPS_WORKSPACE_ID = 'f0f0f0f0-0000-0000-0000-000000000001';

    /** Mirror of the silver.decision_records.decision_type CHECK constraint. */
    public const DECISION_TYPES = [
        'target_recommendation',
        'crs_decision',
        'schema_mapping',
        'public_data_import',
        'export_approval',
        'workflow_enablement',
        'conflict_resolution',
        'report_signoff',
    ];

    public function __construct(private readonly AuditEmitter $auditEmitter) {}

    /**
     * Record one §21 decision with all links + audit emission.
     *
     * @param string $workspaceId Workspace RLS scope (UUID).
     * @param string $decisionType One of the 8 §21.3 types.
     * @param string $recommendation AI / system recommendation text.
     * @param string $humanDecision Human's chosen action.
     * @param int $decidedByUserId public.users.id of the decider.
     * @param ?string $reason Optional human rationale.
     * @param ?float $uncertainty Declared uncertainty in [0, 1].
     * @param list<string> $evidenceChunkIds Supporting source chunk ids.
     * @param list<array{label: string, description?: string, was_chosen?: bool, payload?: array<string, mixed>}> $optionsConsidered
     * @param ?string $outcomeKind Optional post-decision outcome kind.
     * @param ?array<string, mixed> $outcomePayload Optional outcome details.
     *
     * @return string The decision_id (UUID).
     *
     * @throws InvalidArgumentException
     */
    public function record(
        string $workspaceId,
        string $decisionType,
        string $recommendation,
        string $humanDecision,
        int $decidedByUserId,
        ?string $reason = null,
        ?float $uncertainty = null,
        array $evidenceChunkIds = [],
        array $optionsConsidered = [],
        ?string $outcomeKind = null,
        ?array $outcomePayload = null,
    ): string {
        if (! in_array($decisionType, self::DECISION_TYPES, true)) {
            throw new InvalidArgumentException(
                'decision_type must be one of: '.implode(', ', self::DECISION_TYPES).". Got: {$decisionType}",
            );
        }
        if ($uncertainty !== null && ($uncertainty < 0 || $uncertainty > 1)) {
            throw new InvalidArgumentException(
                "uncertainty must be in [0, 1] when set; got {$uncertainty}",
            );
        }
        if (preg_match('/^[0-9a-fA-F-]{36}$/', $workspaceId) !== 1) {
            throw new InvalidArgumentException(
                "workspace_id must be a UUID; got {$workspaceId}",
            );
        }

        $decisionId = DB::connection('pgsql')->transaction(function () use (
            $workspaceId,
            $decisionType,
            $recommendation,
            $humanDecision,
            $decidedByUserId,
            $reason,
            $uncertainty,
            $evidenceChunkIds,
            $optionsConsidered,
            $outcomeKind,
            $outcomePayload,
        ): string {
            // 0. Set the `app.workspace_id` GUC for the duration of this
            //    transaction so RLS WITH CHECK on `silver.decision_records`
            //    accepts the INSERT. `set_config(..., true)` is txn-local
            //    in Postgres so it auto-resets on commit / rollback.
            DB::connection('pgsql')->statement(
                "SELECT set_config('app.workspace_id', ?, true)",
                [$workspaceId],
            );

            // 1. INSERT decision_records — get the new decision_id back.
            $row = DB::connection('pgsql')->selectOne(
                <<<'SQL'
                INSERT INTO silver.decision_records (
                    workspace_id, decision_type, recommendation, human_decision,
                    reason, uncertainty, decided_by_user_id
                )
                VALUES (?::uuid, ?, ?, ?, ?, ?, ?)
                RETURNING decision_id::text AS decision_id
                SQL,
                [
                    $workspaceId,
                    $decisionType,
                    $recommendation,
                    $humanDecision,
                    $reason,
                    $uncertainty,
                    $decidedByUserId,
                ],
            );
            $decisionId = (string) $row->decision_id;

            // 2. INSERT evidence links.
            foreach ($evidenceChunkIds as $chunkId) {
                DB::connection('pgsql')->statement(
                    <<<'SQL'
                    INSERT INTO silver.decision_evidence_links (
                        decision_id, source_chunk_id, role
                    )
                    VALUES (?::uuid, ?, 'supporting')
                    SQL,
                    [$decisionId, $chunkId],
                );
            }

            // 3. INSERT options considered.
            foreach ($optionsConsidered as $opt) {
                $label = $opt['label'] ?? null;
                if ($label === null || $label === '') {
                    throw new InvalidArgumentException("each option must have a 'label' field");
                }
                $description = (string) ($opt['description'] ?? '');
                $wasChosen = (bool) ($opt['was_chosen'] ?? false);
                $payload = (array) ($opt['payload'] ?? []);

                DB::connection('pgsql')->statement(
                    <<<'SQL'
                    INSERT INTO silver.decision_options (
                        decision_id, label, description, was_chosen, payload
                    )
                    VALUES (?::uuid, ?, ?, ?, ?::jsonb)
                    SQL,
                    [
                        $decisionId,
                        $label,
                        $description,
                        $wasChosen,
                        json_encode($payload, JSON_THROW_ON_ERROR),
                    ],
                );
            }

            // 4. Optional outcome row.
            if ($outcomeKind !== null) {
                DB::connection('pgsql')->statement(
                    <<<'SQL'
                    INSERT INTO silver.decision_outcomes (
                        decision_id, outcome_kind, outcome_payload
                    )
                    VALUES (?::uuid, ?, ?::jsonb)
                    SQL,
                    [
                        $decisionId,
                        $outcomeKind,
                        json_encode($outcomePayload ?? [], JSON_THROW_ON_ERROR),
                    ],
                );
            }

            // 5. Emit audit ledger row.
            $ledger = $this->auditEmitter->emit(
                actionType: 'decision.'.$decisionType,
                workspaceId: $workspaceId,
                actorId: $decidedByUserId,
                actorKind: AuditEmitter::ACTOR_USER,
                targetSchema: 'silver',
                targetTable: 'decision_records',
                targetId: $decisionId,
                payload: [
                    'decision_type' => $decisionType,
                    'human_decision' => $humanDecision,
                    'evidence_count' => count($evidenceChunkIds),
                    'options_count' => count($optionsConsidered),
                ],
            );

            // 6. Back-fill audit_ledger_id + hash on the decision row.
            //    AuditEmitter::emit returns hash as bytea-cast hex; the
            //    Python facade does the same back-fill so we mirror it.
            DB::connection('pgsql')->statement(
                <<<'SQL'
                UPDATE silver.decision_records
                   SET audit_ledger_id = ?::uuid,
                       hash = ?::bytea
                 WHERE decision_id = ?::uuid
                SQL,
                [
                    $ledger['id'],
                    '\\x'.$ledger['hash'],
                    $decisionId,
                ],
            );

            return $decisionId;
        });

        // Phase 5 — broadcast Admin/DecisionHistory refresh AFTER the
        // transaction commits so the page's re-fetch sees the durable row.
        // Best-effort: broadcast failure must not throw past the caller.
        try {
            AdminSurfaceUpdated::dispatch(
                'decision-history',
                null,
                ['recent_decisions', 'recent_audit_anchors', 'kpis'],
                [
                    'decision_id' => $decisionId,
                    'workspace_id' => $workspaceId,
                    'decision_type' => $decisionType,
                    'human_decision' => $humanDecision,
                ],
            );
        } catch (\Throwable $e) {
            Log::warning(
                'RecordDecision: decision-history broadcast failed',
                [
                    'decision_id' => $decisionId,
                    'error' => $e->getMessage(),
                ],
            );
        }

        return $decisionId;
    }
}
