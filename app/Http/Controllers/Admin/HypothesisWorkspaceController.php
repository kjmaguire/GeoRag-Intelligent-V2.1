<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin;

use App\Http\Controllers\Controller;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Master-plan §9.10 — Hypothesis Workspace admin view (doc-phase 131).
 *
 * Read-only admin surface for `silver.hypotheses` +
 * `silver.hypothesis_evidence_links`. The fourth Track-3 admin surface
 * after Eval Dashboard (128), Decision History (129), Support Cockpit
 * (130).
 *
 * Surfaces:
 *
 *   - per-review-status rollup (ai_suggested / reviewed / accepted / rejected)
 *   - per-confidence-method rollup
 *   - evidence-role histogram (supporting / contradicting / missing /
 *     recommended_test)
 *   - last 50 hypotheses (recency-ordered) with workspace + reviewer +
 *     evidence-link counts per role
 *   - last 100 evidence links (cross-hypothesis recency view)
 *
 * Auth: 'admin' Gate. Admin views read cross-workspace; the
 * doc-phase 129 RLS retrofit adds the "GUC unset ⇒ all rows visible"
 * escape hatch to `silver.hypotheses`. The EXISTS-based child policy on
 * `silver.hypothesis_evidence_links` inherits the escape hatch
 * transitively through its parent.
 *
 * Route: GET /admin/hypothesis-workspace
 */
class HypothesisWorkspaceController extends Controller
{
    /** Mirror of the hypotheses_review_status_valid CHECK constraint. */
    private const REVIEW_STATUSES = ['ai_suggested', 'reviewed', 'accepted', 'rejected'];

    /** Mirror of the hypothesis_evidence_links_role_valid CHECK constraint. */
    private const EVIDENCE_ROLES = ['supporting', 'contradicting', 'missing', 'recommended_test'];

    public function index(Request $request): Response
    {
        $this->authorize('admin');

        $filters = $request->validate([
            'review_status' => ['nullable', 'in:'.implode(',', self::REVIEW_STATUSES)],
            'workspace_id'  => ['nullable', 'uuid'],
        ]);

        return Inertia::render('Admin/HypothesisWorkspace', [
            'kpis'                  => $this->kpis(),
            'by_review_status'      => $this->byReviewStatus(),
            'by_confidence_method'  => $this->byConfidenceMethod(),
            'by_evidence_role'      => $this->byEvidenceRole(),
            'recent_hypotheses'     => $this->recentHypotheses($filters),
            'recent_evidence_links' => $this->recentEvidenceLinks(),
            'filters'               => array_filter($filters, fn ($v) => $v !== null),
            'valid_review_statuses' => self::REVIEW_STATUSES,
            'valid_evidence_roles'  => self::EVIDENCE_ROLES,
        ]);
    }

    /**
     * Top-level KPI counters.
     *
     * @return array{
     *   total_hypotheses: int,
     *   accepted_count: int,
     *   ai_suggested_count: int,
     *   mean_confidence: ?float,
     *   distinct_workspaces: int,
     *   distinct_parent_questions: int,
     *   total_evidence_links: int,
     *   recent_30d_count: int,
     *   latest_created_at: ?string,
     * }
     */
    private function kpis(): array
    {
        $h = DB::selectOne(<<<'SQL'
            SELECT
                count(*) AS total,
                count(*) FILTER (WHERE review_status = 'accepted')      AS accepted,
                count(*) FILTER (WHERE review_status = 'ai_suggested')  AS ai_suggested,
                avg(confidence)::float AS mean_confidence,
                count(DISTINCT workspace_id) AS distinct_workspaces,
                count(DISTINCT parent_question) AS distinct_parents,
                count(*) FILTER (WHERE created_at >= now() - interval '30 days') AS recent_30d,
                max(created_at) AS latest_at
            FROM silver.hypotheses
        SQL);

        $e = DB::selectOne(<<<'SQL'
            SELECT count(*) AS n FROM silver.hypothesis_evidence_links
        SQL);

        return [
            'total_hypotheses'           => (int) $h->total,
            'accepted_count'             => (int) $h->accepted,
            'ai_suggested_count'         => (int) $h->ai_suggested,
            'mean_confidence'            => $h->mean_confidence !== null
                ? round((float) $h->mean_confidence, 3)
                : null,
            'distinct_workspaces'        => (int) $h->distinct_workspaces,
            'distinct_parent_questions'  => (int) $h->distinct_parents,
            'total_evidence_links'       => (int) $e->n,
            'recent_30d_count'           => (int) $h->recent_30d,
            'latest_created_at'          => $h->latest_at,
        ];
    }

    /**
     * Per-review-status counts.
     *
     * @return array<int, array{review_status: string, count: int}>
     */
    private function byReviewStatus(): array
    {
        $rows = DB::select(<<<'SQL'
            SELECT review_status, count(*) AS n
            FROM silver.hypotheses
            GROUP BY review_status
            ORDER BY
                CASE review_status
                    WHEN 'ai_suggested' THEN 1
                    WHEN 'reviewed'     THEN 2
                    WHEN 'accepted'     THEN 3
                    WHEN 'rejected'     THEN 4
                    ELSE 5
                END
        SQL);

        return array_map(static fn (object $r) => [
            'review_status' => $r->review_status,
            'count'         => (int) $r->n,
        ], $rows);
    }

    /**
     * Per-confidence-method counts (NULL bucketed as 'unknown').
     *
     * @return array<int, array{confidence_method: string, count: int}>
     */
    private function byConfidenceMethod(): array
    {
        $rows = DB::select(<<<'SQL'
            SELECT coalesce(confidence_method, 'unknown') AS method, count(*) AS n
            FROM silver.hypotheses
            GROUP BY coalesce(confidence_method, 'unknown')
            ORDER BY n DESC
        SQL);

        return array_map(static fn (object $r) => [
            'confidence_method' => $r->method,
            'count'             => (int) $r->n,
        ], $rows);
    }

    /**
     * Evidence-role histogram (cross-hypothesis).
     *
     * @return array<int, array{role: string, count: int}>
     */
    private function byEvidenceRole(): array
    {
        $rows = DB::select(<<<'SQL'
            SELECT role, count(*) AS n
            FROM silver.hypothesis_evidence_links
            GROUP BY role
            ORDER BY
                CASE role
                    WHEN 'supporting'        THEN 1
                    WHEN 'contradicting'     THEN 2
                    WHEN 'missing'           THEN 3
                    WHEN 'recommended_test'  THEN 4
                    ELSE 5
                END
        SQL);

        return array_map(static fn (object $r) => [
            'role'  => $r->role,
            'count' => (int) $r->n,
        ], $rows);
    }

    /**
     * Last 50 hypotheses (filter-aware), with evidence-link counts.
     *
     * @param  array{review_status?: ?string, workspace_id?: ?string}  $filters
     * @return array<int, array{
     *   hypothesis_id: string,
     *   workspace_id: string,
     *   parent_question: string,
     *   label: string,
     *   description: string,
     *   confidence: ?float,
     *   confidence_method: ?string,
     *   review_status: string,
     *   reviewed_by_user_id: ?int,
     *   reviewed_at: ?string,
     *   created_at: string,
     *   supporting_count: int,
     *   contradicting_count: int,
     *   missing_count: int,
     *   recommended_test_count: int,
     * }>
     */
    private function recentHypotheses(array $filters): array
    {
        $where = '';
        $bindings = [];
        if (! empty($filters['review_status'])) {
            $where .= ' AND h.review_status = ?';
            $bindings[] = $filters['review_status'];
        }
        if (! empty($filters['workspace_id'])) {
            $where .= ' AND h.workspace_id = ?';
            $bindings[] = $filters['workspace_id'];
        }

        $rows = DB::select(<<<SQL
            SELECT
                h.hypothesis_id::text         AS hypothesis_id,
                h.workspace_id::text          AS workspace_id,
                left(h.parent_question, 200)  AS parent_question,
                h.label,
                left(h.description, 200)      AS description,
                h.confidence::float           AS confidence,
                h.confidence_method,
                h.review_status,
                h.reviewed_by_user_id,
                h.reviewed_at,
                h.created_at,
                (SELECT count(*) FROM silver.hypothesis_evidence_links l
                  WHERE l.hypothesis_id = h.hypothesis_id AND l.role = 'supporting')        AS supporting_count,
                (SELECT count(*) FROM silver.hypothesis_evidence_links l
                  WHERE l.hypothesis_id = h.hypothesis_id AND l.role = 'contradicting')     AS contradicting_count,
                (SELECT count(*) FROM silver.hypothesis_evidence_links l
                  WHERE l.hypothesis_id = h.hypothesis_id AND l.role = 'missing')           AS missing_count,
                (SELECT count(*) FROM silver.hypothesis_evidence_links l
                  WHERE l.hypothesis_id = h.hypothesis_id AND l.role = 'recommended_test')  AS recommended_test_count
            FROM silver.hypotheses h
            WHERE 1=1 {$where}
            ORDER BY h.created_at DESC
            LIMIT 50
        SQL, $bindings);

        return array_map(static fn (object $r) => [
            'hypothesis_id'           => (string) $r->hypothesis_id,
            'workspace_id'            => (string) $r->workspace_id,
            'parent_question'         => (string) $r->parent_question,
            'label'                   => $r->label,
            'description'             => (string) $r->description,
            'confidence'              => $r->confidence !== null ? round((float) $r->confidence, 3) : null,
            'confidence_method'       => $r->confidence_method,
            'review_status'           => $r->review_status,
            'reviewed_by_user_id'     => $r->reviewed_by_user_id !== null ? (int) $r->reviewed_by_user_id : null,
            'reviewed_at'             => $r->reviewed_at,
            'created_at'              => $r->created_at,
            'supporting_count'        => (int) $r->supporting_count,
            'contradicting_count'     => (int) $r->contradicting_count,
            'missing_count'           => (int) $r->missing_count,
            'recommended_test_count'  => (int) $r->recommended_test_count,
        ], $rows);
    }

    /**
     * Last 100 evidence links (cross-hypothesis recency view).
     *
     * @return array<int, array{
     *   link_id: string,
     *   hypothesis_id: string,
     *   hypothesis_label: string,
     *   workspace_id: string,
     *   source_chunk_id: ?string,
     *   role: string,
     *   weight: ?float,
     * }>
     */
    private function recentEvidenceLinks(): array
    {
        $rows = DB::select(<<<'SQL'
            SELECT
                l.link_id::text          AS link_id,
                l.hypothesis_id::text    AS hypothesis_id,
                h.label                  AS hypothesis_label,
                h.workspace_id::text     AS workspace_id,
                l.source_chunk_id,
                l.role,
                l.weight::float          AS weight
            FROM silver.hypothesis_evidence_links l
            INNER JOIN silver.hypotheses h
                ON h.hypothesis_id = l.hypothesis_id
            ORDER BY h.created_at DESC, l.link_id
            LIMIT 100
        SQL);

        return array_map(static fn (object $r) => [
            'link_id'           => (string) $r->link_id,
            'hypothesis_id'     => (string) $r->hypothesis_id,
            'hypothesis_label'  => $r->hypothesis_label,
            'workspace_id'      => (string) $r->workspace_id,
            'source_chunk_id'   => $r->source_chunk_id,
            'role'              => $r->role,
            'weight'            => $r->weight !== null ? round((float) $r->weight, 3) : null,
        ], $rows);
    }
}
