<?php

declare(strict_types=1);

namespace Tests\Feature\DecisionIntelligence;

use App\Services\DecisionIntelligence\RecordDecision;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Doc-phase 133 — feature coverage for the Laravel-side RecordDecision
 * facade. Mirrors the Python-side test_decision_recorder.py.
 *
 * Verifies:
 *   - Happy path: workflow_enablement decision lands with audit anchor
 *   - Options + evidence_chunk_ids land + are readable post-commit
 *   - Invalid decision_type throws
 *   - Invalid uncertainty range throws
 *   - Outcome row lands when outcomeKind is set
 *
 * Gated on PG test connection (the service writes to silver.* schemas).
 */
class RecordDecisionTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private function cleanupDecision(string $decisionId): void
    {
        // CASCADE drops evidence_links / options / outcomes via FK.
        DB::connection('pgsql')->statement(
            'DELETE FROM silver.decision_records WHERE decision_id = ?::uuid',
            [$decisionId]
        );
    }

    public function test_happy_path_workflow_enablement(): void
    {
        $user = User::factory()->create();
        $svc = app(RecordDecision::class);

        $decisionId = $svc->record(
            workspaceId: RecordDecision::PLATFORM_OPS_WORKSPACE_ID,
            decisionType: 'workflow_enablement',
            recommendation: 'Enable test_flag',
            humanDecision: 'accepted',
            decidedByUserId: $user->id,
            reason: 'feature test happy path',
        );

        $this->assertMatchesRegularExpression('/^[0-9a-f-]{36}$/', $decisionId);

        $row = DB::connection('pgsql')->selectOne(
            'SELECT decision_type, human_decision, reason, audit_ledger_id::text AS audit_id, length(hash) AS h
               FROM silver.decision_records WHERE decision_id = ?::uuid',
            [$decisionId]
        );
        $this->assertNotNull($row);
        $this->assertSame('workflow_enablement', $row->decision_type);
        $this->assertSame('accepted', $row->human_decision);
        $this->assertSame('feature test happy path', $row->reason);
        $this->assertNotNull($row->audit_id);
        $this->assertSame(32, (int) $row->h);  // SHA-256 bytea length

        // Audit ledger row exists with the right action_type.
        $audit = DB::connection('pgsql')->selectOne(
            'SELECT action_type, actor_id FROM audit.audit_ledger WHERE id = ?::uuid',
            [$row->audit_id]
        );
        $this->assertSame('decision.workflow_enablement', $audit->action_type);
        $this->assertSame($user->id, (int) $audit->actor_id);

        $this->cleanupDecision($decisionId);
    }

    public function test_options_and_evidence_persist(): void
    {
        $user = User::factory()->create();
        $svc = app(RecordDecision::class);

        $decisionId = $svc->record(
            workspaceId: RecordDecision::PLATFORM_OPS_WORKSPACE_ID,
            decisionType: 'workflow_enablement',
            recommendation: 'Enable feature_a',
            humanDecision: 'accepted',
            decidedByUserId: $user->id,
            evidenceChunkIds: ['chunk_a', 'chunk_b'],
            optionsConsidered: [
                ['label' => 'enable', 'description' => 'Enable', 'was_chosen' => true],
                ['label' => 'disable', 'description' => 'Disable', 'was_chosen' => false],
            ],
        );

        $links = DB::connection('pgsql')->select(
            "SELECT source_chunk_id FROM silver.decision_evidence_links
              WHERE decision_id = ?::uuid ORDER BY source_chunk_id",
            [$decisionId]
        );
        $this->assertCount(2, $links);
        $this->assertSame('chunk_a', $links[0]->source_chunk_id);
        $this->assertSame('chunk_b', $links[1]->source_chunk_id);

        $opts = DB::connection('pgsql')->select(
            "SELECT label, was_chosen FROM silver.decision_options
              WHERE decision_id = ?::uuid ORDER BY label",
            [$decisionId]
        );
        $this->assertCount(2, $opts);
        $this->assertSame('disable', $opts[0]->label);
        $this->assertFalse((bool) $opts[0]->was_chosen);
        $this->assertSame('enable', $opts[1]->label);
        $this->assertTrue((bool) $opts[1]->was_chosen);

        $this->cleanupDecision($decisionId);
    }

    public function test_outcome_persists_when_kind_provided(): void
    {
        $user = User::factory()->create();
        $svc = app(RecordDecision::class);

        $decisionId = $svc->record(
            workspaceId: RecordDecision::PLATFORM_OPS_WORKSPACE_ID,
            decisionType: 'workflow_enablement',
            recommendation: 'Enable feature_b',
            humanDecision: 'accepted',
            decidedByUserId: $user->id,
            outcomeKind: 'enabled',
            outcomePayload: ['flag' => 'feature_b'],
        );

        $outcome = DB::connection('pgsql')->selectOne(
            "SELECT outcome_kind, outcome_payload::text AS payload
               FROM silver.decision_outcomes WHERE decision_id = ?::uuid",
            [$decisionId]
        );
        $this->assertNotNull($outcome);
        $this->assertSame('enabled', $outcome->outcome_kind);
        $this->assertStringContainsString('feature_b', $outcome->payload);

        $this->cleanupDecision($decisionId);
    }

    public function test_invalid_decision_type_throws(): void
    {
        $user = User::factory()->create();
        $svc = app(RecordDecision::class);

        $this->expectException(\InvalidArgumentException::class);
        $svc->record(
            workspaceId: RecordDecision::PLATFORM_OPS_WORKSPACE_ID,
            decisionType: 'not_a_real_type',
            recommendation: 'x',
            humanDecision: 'accepted',
            decidedByUserId: $user->id,
        );
    }

    public function test_uncertainty_out_of_range_throws(): void
    {
        $user = User::factory()->create();
        $svc = app(RecordDecision::class);

        $this->expectException(\InvalidArgumentException::class);
        $svc->record(
            workspaceId: RecordDecision::PLATFORM_OPS_WORKSPACE_ID,
            decisionType: 'workflow_enablement',
            recommendation: 'x',
            humanDecision: 'accepted',
            decidedByUserId: $user->id,
            uncertainty: 1.5,
        );
    }

    public function test_invalid_workspace_id_throws(): void
    {
        $user = User::factory()->create();
        $svc = app(RecordDecision::class);

        $this->expectException(\InvalidArgumentException::class);
        $svc->record(
            workspaceId: 'not-a-uuid',
            decisionType: 'workflow_enablement',
            recommendation: 'x',
            humanDecision: 'accepted',
            decidedByUserId: $user->id,
        );
    }
}
