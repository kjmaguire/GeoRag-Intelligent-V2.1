<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin;

use App\Http\Controllers\Controller;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Master-plan §10.7 — Eval Dashboard (doc-phase 128).
 *
 * Read-only admin surface for the §10 eval harness. Surfaces:
 *
 *   - golden-question population (per question_set + per status)
 *   - SME-pass progress (mechanical-seeded vs SME-authored split)
 *   - geological-ontology population progress (12 classes × per-class status)
 *   - recent eval runs (eval.run_summaries) — pass/fail/regression counts
 *
 * Auth: 'admin' Gate (users.is_admin = true).
 *
 * Route: GET /admin/eval-dashboard
 *
 * Backing data sources:
 *   - eval.golden_questions  (doc-phase 97 schema + 124 seeded mechanical)
 *   - eval.run_summaries     (populated by §10.4 evaluate_workspace workflow)
 *   - silver.geological_ontology_terms (doc-phase 90 schema + 112 seeded)
 *
 * The dashboard is intentionally Laravel-side (Eloquent + raw SQL) — the
 * §10.6 promotion-gate enforcer lives in FastAPI, but the dashboard
 * surface only needs read access to the rolled-up tables.
 */
class EvalDashboardController extends Controller
{
    public function index(Request $request): Response
    {
        $this->authorize('admin');

        return Inertia::render('Admin/EvalDashboard', [
            'kpis' => $this->kpis(),
            'questions_by_set' => $this->questionsByQuestionSet(),
            'questions_by_difficulty' => $this->questionsByDifficulty(),
            'ontology_progress' => $this->ontologyProgress(),
            'recent_runs' => $this->recentRuns(),
            // Doc-phase 171 — §04i failure-layer breakdown. With all 6
            // §04i validators graduated (doc-phase 168) plus the nightly
            // real_rag_v1 cron (doc-phase 170), per-layer fail counts
            // become the operator's primary triage surface.
            'failure_layer_breakdown' => $this->failureLayerBreakdown(),
        ]);
    }

    /**
     * Top-level KPI cards.
     *
     * @return array{
     *   total_active_questions: int,
     *   total_draft_questions: int,
     *   total_retired_questions: int,
     *   total_ontology_terms: int,
     *   total_ontology_synonyms: int,
     *   ontology_classes_populated: int,
     *   ontology_classes_total: int,
     *   recent_runs_count_30d: int,
     *   last_run_at: ?string,
     * }
     */
    private function kpis(): array
    {
        $q = DB::selectOne(<<<'SQL'
            SELECT
                count(*) FILTER (WHERE status = 'active')   AS active,
                count(*) FILTER (WHERE status = 'draft')    AS draft,
                count(*) FILTER (WHERE status = 'retired')  AS retired
            FROM eval.golden_questions
        SQL);

        $t = DB::selectOne(<<<'SQL'
            SELECT count(*) AS terms FROM silver.geological_ontology_terms
        SQL);

        $s = DB::selectOne(<<<'SQL'
            SELECT count(*) AS syns FROM silver.geological_ontology_synonyms
        SQL);

        $oc = DB::selectOne(<<<'SQL'
            SELECT count(DISTINCT class) AS populated
            FROM silver.geological_ontology_terms
        SQL);

        $rr = DB::selectOne(<<<'SQL'
            SELECT count(*) AS n, max(started_at) AS last_at
            FROM eval.run_summaries
            WHERE started_at >= now() - interval '30 days'
        SQL);

        return [
            'total_active_questions' => (int) $q->active,
            'total_draft_questions' => (int) $q->draft,
            'total_retired_questions' => (int) $q->retired,
            'total_ontology_terms' => (int) $t->terms,
            'total_ontology_synonyms' => (int) $s->syns,
            'ontology_classes_populated' => (int) $oc->populated,
            'ontology_classes_total' => 12,  // per §20.1
            'recent_runs_count_30d' => (int) $rr->n,
            'last_run_at' => $rr->last_at,
        ];
    }

    /**
     * Golden questions grouped by question_set + status.
     *
     * @return array<int, array{
     *   question_set: string,
     *   active: int,
     *   draft: int,
     *   retired: int,
     *   total: int,
     * }>
     */
    private function questionsByQuestionSet(): array
    {
        $rows = DB::select(<<<'SQL'
            SELECT
                question_set,
                count(*) FILTER (WHERE status = 'active')  AS active,
                count(*) FILTER (WHERE status = 'draft')   AS draft,
                count(*) FILTER (WHERE status = 'retired') AS retired,
                count(*) AS total
            FROM eval.golden_questions
            GROUP BY question_set
            ORDER BY question_set
        SQL);

        return array_map(static fn (object $r) => [
            'question_set' => $r->question_set,
            'active' => (int) $r->active,
            'draft' => (int) $r->draft,
            'retired' => (int) $r->retired,
            'total' => (int) $r->total,
        ], $rows);
    }

    /**
     * Golden questions grouped by difficulty.
     *
     * @return array<int, array{difficulty: string, count: int}>
     */
    private function questionsByDifficulty(): array
    {
        $rows = DB::select(<<<'SQL'
            SELECT difficulty, count(*) AS n
            FROM eval.golden_questions
            WHERE status = 'active'
            GROUP BY difficulty
            ORDER BY
                CASE difficulty
                    WHEN 'easy'   THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'hard'   THEN 3
                    ELSE 4
                END
        SQL);

        return array_map(static fn (object $r) => [
            'difficulty' => $r->difficulty,
            'count' => (int) $r->n,
        ], $rows);
    }

    /**
     * Per-class ontology population progress.
     *
     * Mirrors the §20.1 12-class taxonomy. Status heuristic matches
     * `app.services.geological_ontology.get_ontology_class_stats`:
     *   - 'empty'              — 0 terms
     *   - 'mechanical_seeded'  — ≥1 term in a mechanical class (commodity,
     *                            geological_age, resource_class), below
     *                            class threshold
     *   - 'sme_populating'     — ≥1 term in an SME class, below threshold
     *   - 'populated'          — term_count ≥ class threshold
     *
     * @return array<int, array{
     *   ontology_class: string,
     *   term_count: int,
     *   synonym_count: int,
     *   status: string,
     *   threshold: int,
     * }>
     */
    private function ontologyProgress(): array
    {
        // Threshold floors per the §9 scope proposal (mirrored in the
        // FastAPI _POPULATED_THRESHOLDS dict).
        $thresholds = [
            'commodity' => 30,
            'geological_age' => 20,
            'resource_class' => 6,
            'deposit_model' => 8,
            'lithology' => 150,
            'alteration' => 25,
            'structure' => 15,
            'mineral_assemblage' => 20,
            'host_rock' => 20,
            'tectonic_setting' => 12,
            'geochemistry' => 10,
            'geophysics' => 10,
        ];
        $mechanical = ['commodity', 'geological_age', 'resource_class'];

        $rows = DB::select(<<<'SQL'
            WITH counts AS (
                SELECT t.class, count(*) AS term_count
                FROM silver.geological_ontology_terms t
                GROUP BY t.class
            ),
            syn_counts AS (
                SELECT t.class, count(*) AS synonym_count
                FROM silver.geological_ontology_synonyms s
                JOIN silver.geological_ontology_terms t ON t.term_id = s.term_id
                GROUP BY t.class
            )
            SELECT
                c.class,
                COALESCE(co.term_count, 0)    AS term_count,
                COALESCE(sc.synonym_count, 0) AS synonym_count
            FROM unnest(ARRAY[
                'deposit_model','commodity','lithology','alteration','structure',
                'mineral_assemblage','host_rock','geological_age','tectonic_setting',
                'geochemistry','geophysics','resource_class'
            ]) AS c(class)
            LEFT JOIN counts co     ON co.class = c.class
            LEFT JOIN syn_counts sc ON sc.class = c.class
            ORDER BY c.class
        SQL);

        return array_map(static function (object $r) use ($thresholds, $mechanical): array {
            $cls = $r->class;
            $tc = (int) $r->term_count;
            $thr = $thresholds[$cls] ?? 1;

            if ($tc === 0) {
                $status = 'empty';
            } elseif ($tc >= $thr) {
                $status = 'populated';
            } elseif (in_array($cls, $mechanical, true)) {
                $status = 'mechanical_seeded';
            } else {
                $status = 'sme_populating';
            }

            return [
                'ontology_class' => $cls,
                'term_count' => $tc,
                'synonym_count' => (int) $r->synonym_count,
                'status' => $status,
                'threshold' => $thr,
            ];
        }, $rows);
    }

    /**
     * Recent eval runs (last 30 days).
     *
     * @return array<int, array{
     *   run_id: string,
     *   triggered_by: string,
     *   question_set_filter: ?string,
     *   question_count: int,
     *   pass_count: int,
     *   fail_count: int,
     *   regression_count: int,
     *   blocks_promotion: bool,
     *   started_at: string,
     *   completed_at: ?string,
     * }>
     */
    private function recentRuns(): array
    {
        // Doc-phase 164 — extract evaluator_kind from trigger_payload.
        // Defaults to 'synthetic_stub' for runs from doc-phase 132/142
        // that predate the field. Coalesce keeps the dashboard rendering
        // even when older rows are mixed in.
        $rows = DB::select(<<<'SQL'
            SELECT run_id, triggered_by, question_set_filter,
                   question_count, pass_count, fail_count, regression_count,
                   blocks_promotion, started_at, completed_at,
                   COALESCE(trigger_payload->>'evaluator_kind', 'synthetic_stub')
                       AS evaluator_kind
            FROM eval.run_summaries
            WHERE started_at >= now() - interval '30 days'
            ORDER BY started_at DESC
            LIMIT 20
        SQL);

        return array_map(static fn (object $r) => [
            'run_id' => (string) $r->run_id,
            'triggered_by' => $r->triggered_by,
            'question_set_filter' => $r->question_set_filter,
            'question_count' => (int) $r->question_count,
            'pass_count' => (int) $r->pass_count,
            'fail_count' => (int) $r->fail_count,
            'regression_count' => (int) $r->regression_count,
            'blocks_promotion' => (bool) $r->blocks_promotion,
            'started_at' => $r->started_at,
            'completed_at' => $r->completed_at,
            'evaluator_kind' => (string) $r->evaluator_kind,
        ], $rows);
    }

    /**
     * §04i failure-layer breakdown over the past 30 days (doc-phase 171).
     *
     * Aggregates `eval.run_results.failure_layer` across failed rows. The
     * layer bucket names match the validators module:
     *
     *   - 1_retrieval_quality   (Layer 1, doc-phase 168)
     *   - 2_citation_presence   (Layer 2, doc-phase 163)
     *   - 3_numeric_claims      (Layer 3, doc-phase 167)
     *   - 4_entity_resolution   (Layer 4, doc-phase 166)
     *   - 5_chunk_provenance    (Layer 5, doc-phase 165)
     *   - 6_refusal             (Layer 6, doc-phase 159 / 163)
     *   - refusal               (legacy bucket from real_llm_v1 evaluator
     *                            pre-doc-phase-163; mapped to layer 6 in
     *                            the UI)
     *   - evaluator_not_ready   (infrastructure — vLLM unreachable, etc.)
     *
     * All buckets are returned even when count=0 so the operator panel
     * renders a complete §04i layer-map every day, not just the failing
     * ones. Zero counts get a muted "—" treatment in the UI.
     *
     * @return array<int, array{
     *   failure_layer: string,
     *   fail_count: int,
     *   last_failed_at: ?string,
     * }>
     */
    private function failureLayerBreakdown(): array
    {
        // Canonical layer order — matches the chain order in real_rag_v1.
        $canonical = [
            '6_refusal',
            '2_citation_presence',
            '5_chunk_provenance',
            '4_entity_resolution',
            '3_numeric_claims',
            '1_retrieval_quality',
            'refusal',
            'evaluator_not_ready',
        ];

        $rows = DB::select(<<<'SQL'
            SELECT failure_layer,
                   count(*) AS fail_count,
                   max(rr.executed_at) AS last_failed_at
            FROM eval.run_results rr
            JOIN eval.run_summaries rs ON rs.run_id = rr.run_id
            WHERE rr.failure_layer IS NOT NULL
              AND rs.started_at >= now() - interval '30 days'
            GROUP BY failure_layer
        SQL);

        $byLayer = [];
        foreach ($rows as $r) {
            $byLayer[$r->failure_layer] = [
                'fail_count' => (int) $r->fail_count,
                'last_failed_at' => $r->last_failed_at,
            ];
        }

        $out = [];
        foreach ($canonical as $layer) {
            $bucket = $byLayer[$layer] ?? ['fail_count' => 0, 'last_failed_at' => null];
            $out[] = [
                'failure_layer' => $layer,
                'fail_count' => $bucket['fail_count'],
                'last_failed_at' => $bucket['last_failed_at'],
            ];
        }

        // Any non-canonical buckets (forward-compat — future validators)
        // surface at the end so they don't get silently dropped.
        foreach ($byLayer as $layer => $bucket) {
            if (! in_array($layer, $canonical, true)) {
                $out[] = [
                    'failure_layer' => $layer,
                    'fail_count' => $bucket['fail_count'],
                    'last_failed_at' => $bucket['last_failed_at'],
                ];
            }
        }

        return $out;
    }
}
