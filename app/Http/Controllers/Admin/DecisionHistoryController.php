<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin;

use App\Http\Controllers\Controller;
use App\Services\DecisionIntelligence\RecordDecision;
use Illuminate\Http\RedirectResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Auth;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Master-plan §9.12 — Decision History admin view (doc-phase 129).
 *
 * Read-only admin surface for `silver.decision_records` + the
 * `audit.audit_ledger` entries it anchors. Surfaces:
 *
 *   - per-decision-type aggregates (8 §21.3 types)
 *   - per-human-decision rollups (accepted / modified / rejected / signed_off / other)
 *   - audit-anchor coverage (% of decisions with an audit_ledger_id)
 *   - mean uncertainty across decisions
 *   - last 50 decisions (recency-ordered) with workspace + decided_by
 *   - last 100 audit_ledger rows scoped to action_type LIKE 'decision.%'
 *
 * Auth: 'admin' Gate. Admin views read cross-workspace; relies on the
 * doc-phase 129 RLS retrofit that adds the "GUC unset ⇒ all rows
 * visible" escape hatch to `silver.decision_records`.
 *
 * Route: GET /admin/decision-history
 *
 * Companion to:
 *   - app.services.decision_intelligence.get_workspace_decision_summary
 *     (per-workspace FastAPI-side aggregator; live since doc-phase 119)
 *   - app.audit.get_workspace_audit_excerpt (paginated audit reader;
 *     live since doc-phase 121)
 */
class DecisionHistoryController extends Controller
{
    /** 8 §21.3 decision types (mirror of the CHECK constraint). */
    private const DECISION_TYPES = [
        'target_recommendation',
        'crs_decision',
        'schema_mapping',
        'public_data_import',
        'export_approval',
        'workflow_enablement',
        'conflict_resolution',
        'report_signoff',
    ];

    /** 4 canonical human_decision values + 'other' bucket. */
    private const HUMAN_DECISIONS = ['accepted', 'modified', 'rejected', 'signed_off'];

    public function index(Request $request): Response
    {
        $this->authorize('admin');

        $filters = $request->validate([
            'decision_type' => ['nullable', 'in:'.implode(',', self::DECISION_TYPES)],
            'workspace_id' => ['nullable', 'uuid'],
        ]);

        return Inertia::render('Admin/DecisionHistory', [
            'kpis' => $this->kpis(),
            'by_decision_type' => $this->byDecisionType(),
            'by_human_decision' => $this->byHumanDecision(),
            'recent_decisions' => $this->recentDecisions($filters),
            'recent_audit_anchors' => $this->recentAuditAnchors(),
            'filters' => array_filter($filters, fn ($v) => $v !== null),
            'valid_decision_types' => self::DECISION_TYPES,
        ]);
    }

    /**
     * Doc-phase 158 — manual §21.3 decision-entry page.
     *
     * Renders /admin/decisions/new where an admin can authentically
     * record a §21 decision of any of the 8 types. Closes the
     * "human decision capture" gap for decision types whose parent
     * flows don't have human-facing UI yet — admins file the decision
     * here directly with provenance (reason + uncertainty + chosen options).
     *
     * Route: GET /admin/decisions/new
     */
    public function create(): Response
    {
        $this->authorize('admin');

        return Inertia::render('Admin/DecisionNew', [
            'valid_decision_types' => self::DECISION_TYPES,
            'valid_human_decisions' => self::HUMAN_DECISIONS,
            'platform_ops_workspace_id' => RecordDecision::PLATFORM_OPS_WORKSPACE_ID,
        ]);
    }

    /**
     * Doc-phase 158 — POST /admin/decisions to file a §21.3 decision.
     *
     * Wires to App\Services\DecisionIntelligence\RecordDecision::record()
     * (doc-phase 133). Each submission produces:
     *   - a silver.decision_records row
     *   - 0..N silver.decision_evidence_links rows
     *   - 0..N silver.decision_options rows
     *   - an audit.audit_ledger row with action_type='decision.<type>'
     *     + back-fill of audit_ledger_id + hash on the decision row
     */
    public function store(Request $request, RecordDecision $svc): RedirectResponse
    {
        $this->authorize('admin');

        $validated = $request->validate([
            'workspace_id' => ['nullable', 'uuid'],
            'decision_type' => ['required', 'in:'.implode(',', self::DECISION_TYPES)],
            'recommendation' => ['required', 'string', 'max:1000'],
            'human_decision' => ['required', 'in:'.implode(',', self::HUMAN_DECISIONS)],
            'reason' => ['nullable', 'string', 'max:2000'],
            'uncertainty' => ['nullable', 'numeric', 'between:0,1'],
            'evidence_chunk_ids' => ['nullable', 'array'],
            'evidence_chunk_ids.*' => ['string', 'max:200'],
            'options_considered' => ['nullable', 'array'],
            'options_considered.*.label' => ['required_with:options_considered', 'string', 'max:200'],
            'options_considered.*.description' => ['nullable', 'string', 'max:1000'],
            'options_considered.*.was_chosen' => ['nullable', 'boolean'],
        ]);

        $workspaceId = $validated['workspace_id']
            ?? RecordDecision::PLATFORM_OPS_WORKSPACE_ID;

        $decisionId = $svc->record(
            workspaceId: $workspaceId,
            decisionType: $validated['decision_type'],
            recommendation: $validated['recommendation'],
            humanDecision: $validated['human_decision'],
            decidedByUserId: (int) Auth::id(),
            reason: $validated['reason'] ?? null,
            uncertainty: isset($validated['uncertainty']) ? (float) $validated['uncertainty'] : null,
            evidenceChunkIds: $validated['evidence_chunk_ids'] ?? [],
            optionsConsidered: $validated['options_considered'] ?? [],
        );

        return redirect()
            ->route('admin.decision-history')
            ->with('flash', sprintf(
                '%s decision recorded (%s)',
                $validated['decision_type'],
                substr($decisionId, 0, 8),
            ));
    }

    /**
     * Top-level KPI counters.
     *
     * @return array{
     *   total_decisions: int,
     *   decisions_with_audit_anchor: int,
     *   audit_anchor_pct: float,
     *   mean_uncertainty: ?float,
     *   distinct_workspaces: int,
     *   distinct_deciders: int,
     *   recent_30d_count: int,
     *   latest_decided_at: ?string,
     * }
     */
    private function kpis(): array
    {
        $r = DB::selectOne(<<<'SQL'
            SELECT
                count(*) AS total,
                count(*) FILTER (WHERE audit_ledger_id IS NOT NULL) AS with_anchor,
                avg(uncertainty)::float AS mean_uncertainty,
                count(DISTINCT workspace_id) AS distinct_workspaces,
                count(DISTINCT decided_by_user_id) AS distinct_deciders,
                count(*) FILTER (WHERE decided_at >= now() - interval '30 days') AS recent_30d,
                max(decided_at) AS latest_at
            FROM silver.decision_records
        SQL);

        $total = (int) $r->total;
        $withAnchor = (int) $r->with_anchor;
        $pct = $total > 0 ? round(100.0 * $withAnchor / $total, 1) : 0.0;

        return [
            'total_decisions' => $total,
            'decisions_with_audit_anchor' => $withAnchor,
            'audit_anchor_pct' => $pct,
            'mean_uncertainty' => $r->mean_uncertainty !== null
                ? round((float) $r->mean_uncertainty, 3)
                : null,
            'distinct_workspaces' => (int) $r->distinct_workspaces,
            'distinct_deciders' => (int) $r->distinct_deciders,
            'recent_30d_count' => (int) $r->recent_30d,
            'latest_decided_at' => $r->latest_at,
        ];
    }

    /**
     * Per-decision-type breakdown.
     *
     * @return array<int, array{
     *   decision_type: string,
     *   total: int,
     *   accepted: int,
     *   modified: int,
     *   rejected: int,
     *   signed_off: int,
     *   other: int,
     * }>
     */
    private function byDecisionType(): array
    {
        $rows = DB::select(<<<'SQL'
            SELECT
                decision_type,
                count(*)                                                        AS total,
                count(*) FILTER (WHERE human_decision = 'accepted')             AS accepted,
                count(*) FILTER (WHERE human_decision = 'modified')             AS modified,
                count(*) FILTER (WHERE human_decision = 'rejected')             AS rejected,
                count(*) FILTER (WHERE human_decision = 'signed_off')           AS signed_off,
                count(*) FILTER (
                    WHERE human_decision NOT IN ('accepted','modified','rejected','signed_off')
                )                                                               AS other
            FROM silver.decision_records
            GROUP BY decision_type
            ORDER BY decision_type
        SQL);

        return array_map(static fn (object $r) => [
            'decision_type' => $r->decision_type,
            'total' => (int) $r->total,
            'accepted' => (int) $r->accepted,
            'modified' => (int) $r->modified,
            'rejected' => (int) $r->rejected,
            'signed_off' => (int) $r->signed_off,
            'other' => (int) $r->other,
        ], $rows);
    }

    /**
     * Total counts per human_decision value (cross-type rollup).
     *
     * @return array<int, array{human_decision: string, count: int}>
     */
    private function byHumanDecision(): array
    {
        $rows = DB::select(<<<'SQL'
            SELECT
                CASE WHEN human_decision IN ('accepted','modified','rejected','signed_off')
                    THEN human_decision
                    ELSE 'other'
                END AS bucket,
                count(*) AS n
            FROM silver.decision_records
            GROUP BY bucket
            ORDER BY n DESC
        SQL);

        return array_map(static fn (object $r) => [
            'human_decision' => $r->bucket,
            'count' => (int) $r->n,
        ], $rows);
    }

    /**
     * Last 50 decisions (filter-aware).
     *
     * @param array{decision_type?: ?string, workspace_id?: ?string} $filters
     *
     * @return array<int, array{
     *   decision_id: string,
     *   workspace_id: string,
     *   decision_type: string,
     *   recommendation: string,
     *   human_decision: string,
     *   uncertainty: ?float,
     *   has_audit_anchor: bool,
     *   decided_at: string,
     *   decided_by_user_id: int,
     * }>
     */
    private function recentDecisions(array $filters): array
    {
        $where = '';
        $bindings = [];
        if (! empty($filters['decision_type'])) {
            $where .= ' AND decision_type = ?';
            $bindings[] = $filters['decision_type'];
        }
        if (! empty($filters['workspace_id'])) {
            $where .= ' AND workspace_id = ?';
            $bindings[] = $filters['workspace_id'];
        }

        $rows = DB::select(<<<SQL
            SELECT
                decision_id::text       AS decision_id,
                workspace_id::text      AS workspace_id,
                decision_type,
                left(recommendation, 200) AS recommendation,
                human_decision,
                uncertainty::float      AS uncertainty,
                (audit_ledger_id IS NOT NULL) AS has_audit_anchor,
                decided_at,
                decided_by_user_id
            FROM silver.decision_records
            WHERE 1=1 {$where}
            ORDER BY decided_at DESC
            LIMIT 50
        SQL, $bindings);

        return array_map(static fn (object $r) => [
            'decision_id' => (string) $r->decision_id,
            'workspace_id' => (string) $r->workspace_id,
            'decision_type' => $r->decision_type,
            'recommendation' => (string) $r->recommendation,
            'human_decision' => $r->human_decision,
            'uncertainty' => $r->uncertainty !== null ? round((float) $r->uncertainty, 3) : null,
            'has_audit_anchor' => (bool) $r->has_audit_anchor,
            'decided_at' => $r->decided_at,
            'decided_by_user_id' => (int) $r->decided_by_user_id,
        ], $rows);
    }

    /**
     * Last 100 audit_ledger rows scoped to decision.* action_types.
     *
     * @return array<int, array{
     *   id: string,
     *   action_type: string,
     *   actor_id: ?int,
     *   target_id: ?string,
     *   workspace_id: ?string,
     *   created_at: string,
     * }>
     */
    private function recentAuditAnchors(): array
    {
        $rows = DB::select(<<<'SQL'
            SELECT
                id::text             AS id,
                action_type,
                actor_id,
                target_id,
                workspace_id::text   AS workspace_id,
                created_at
            FROM audit.audit_ledger
            WHERE action_type LIKE 'decision.%'
            ORDER BY created_at DESC
            LIMIT 100
        SQL);

        return array_map(static fn (object $r) => [
            'id' => (string) $r->id,
            'action_type' => $r->action_type,
            'actor_id' => $r->actor_id !== null ? (int) $r->actor_id : null,
            'target_id' => $r->target_id,
            'workspace_id' => $r->workspace_id,
            'created_at' => $r->created_at,
        ], $rows);
    }
}
