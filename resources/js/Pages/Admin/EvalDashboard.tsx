import type { JSX } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

/**
 * /admin/eval-dashboard — Master-plan §10.7 Eval Dashboard
 * (doc-phase 128).
 *
 * Surfaces:
 *   - golden-question population (per question_set + per status)
 *   - SME-pass progress (mechanical vs SME-authored split)
 *   - ontology population progress (12 classes × per-class status)
 *   - recent eval runs (last 30 days)
 *
 * Backend: app/Http/Controllers/Admin/EvalDashboardController.php.
 */

interface KPIs {
    total_active_questions: number;
    total_draft_questions: number;
    total_retired_questions: number;
    total_ontology_terms: number;
    total_ontology_synonyms: number;
    ontology_classes_populated: number;
    ontology_classes_total: number;
    recent_runs_count_30d: number;
    last_run_at: string | null;
}

interface QuestionsBySet {
    question_set: string;
    active: number;
    draft: number;
    retired: number;
    total: number;
}

interface QuestionsByDifficulty {
    difficulty: string;
    count: number;
}

interface OntologyProgress {
    ontology_class: string;
    term_count: number;
    synonym_count: number;
    status: 'empty' | 'mechanical_seeded' | 'sme_populating' | 'populated';
    threshold: number;
}

interface RunSummary {
    run_id: string;
    triggered_by: string;
    question_set_filter: string | null;
    question_count: number;
    pass_count: number;
    fail_count: number;
    regression_count: number;
    blocks_promotion: boolean;
    started_at: string;
    completed_at: string | null;
    /** Doc-phase 164 — which evaluator produced this run. */
    evaluator_kind: string;
}

/** Doc-phase 171 — §04i failure-layer breakdown panel. */
interface FailureLayerBucket {
    failure_layer: string;
    fail_count: number;
    last_failed_at: string | null;
}

interface PageProps {
    kpis: KPIs;
    questions_by_set: QuestionsBySet[];
    questions_by_difficulty: QuestionsByDifficulty[];
    ontology_progress: OntologyProgress[];
    recent_runs: RunSummary[];
    failure_layer_breakdown: FailureLayerBucket[];
}

/**
 * Map a failure_layer bucket id to a human label + tone.
 *
 * The §04i layer numbers correspond to the validators module:
 *   1 retrieval_quality   2 citation_presence   3 numeric_claims
 *   4 entity_resolution   5 chunk_provenance    6 refusal_correctness
 */
function layerMeta(layer: string): { label: string; tone: string; kind: 'layer' | 'infra' } {
    const map: Record<string, { label: string; tone: string; kind: 'layer' | 'infra' }> = {
        '1_retrieval_quality': {
            label: 'Layer 1 — retrieval_quality',
            tone: 'sky',
            kind: 'layer',
        },
        '2_citation_presence': {
            label: 'Layer 2 — citation_presence',
            tone: 'sky',
            kind: 'layer',
        },
        '3_numeric_claims': {
            label: 'Layer 3 — numeric_claims',
            tone: 'sky',
            kind: 'layer',
        },
        '4_entity_resolution': {
            label: 'Layer 4 — entity_resolution',
            tone: 'sky',
            kind: 'layer',
        },
        '5_chunk_provenance': {
            label: 'Layer 5 — chunk_provenance',
            tone: 'sky',
            kind: 'layer',
        },
        '6_refusal': {
            label: 'Layer 6 — refusal_correctness',
            tone: 'sky',
            kind: 'layer',
        },
        refusal: {
            label: 'Layer 6 — refusal (legacy)',
            tone: 'stone',
            kind: 'layer',
        },
        evaluator_not_ready: {
            label: 'Infra — evaluator_not_ready',
            tone: 'amber',
            kind: 'infra',
        },
    };
    return map[layer] ?? { label: layer, tone: 'stone', kind: 'infra' };
}

function formatDate(iso: string | null): string {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleString();
    } catch {
        return iso;
    }
}

function statusBadge(status: string): JSX.Element {
    const map: Record<string, string> = {
        populated: 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300',
        mechanical_seeded: 'border-sky-500/40 bg-sky-500/15 text-sky-300',
        sme_populating: 'border-amber-500/40 bg-amber-500/15 text-amber-300',
        empty: 'border-stone-700 bg-stone-800/40 text-stone-400',
    };
    const cls = map[status] ?? map.empty;
    return (
        <span className={`rounded border px-2 py-0.5 text-xs ${cls}`}>{status}</span>
    );
}

export default function EvalDashboard({
    kpis,
    questions_by_set,
    questions_by_difficulty,
    ontology_progress,
    recent_runs,
    failure_layer_breakdown,
}: PageProps): JSX.Element {
    // Phase 5 real-time push — eval_real_rag_nightly + evaluate_workspace
    // both broadcast `eval-dashboard` on completion. Refresh just the
    // counters + recent_runs list; ontology/question-set rollups change
    // on a different cadence (manual SME pushes).
    useAdminSurfaceUpdated('eval-dashboard', null, () => {
        router.reload({ only: ['kpis', 'recent_runs', 'failure_layer_breakdown'] });
    });

    const totalLayerFails = failure_layer_breakdown.reduce(
        (sum, b) => sum + b.fail_count,
        0,
    );
    const peakLayerFails = failure_layer_breakdown.reduce(
        (max, b) => (b.fail_count > max ? b.fail_count : max),
        0,
    );
    return (
        <AppLayout>
            <Head title="Eval Dashboard — Admin" />
            <div className="min-h-screen bg-stone-950 text-stone-100">
                <div
                    className="mx-auto max-w-7xl px-6 py-8"
                    data-testid="eval-dashboard"
                >
                    <Link
                        href="/dashboard"
                        className="mb-4 inline-block text-sm text-stone-400 hover:text-amber-300"
                    >
                        ← Back to dashboard
                    </Link>

                    <header className="mb-6">
                        <h1 className="text-2xl font-semibold text-stone-50">
                            Eval Dashboard
                        </h1>
                        <p className="mt-1 text-sm text-stone-400">
                            Golden-question population, SME-pass progress, ontology
                            coverage, and recent eval-harness runs. Read-only.
                            Master-plan §10.7.
                        </p>
                    </header>

                    {/* KPI tiles */}
                    <section className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
                        <Tile
                            label="Active golden questions"
                            value={String(kpis.total_active_questions)}
                            tone={kpis.total_active_questions > 0 ? 'good' : 'neutral'}
                        />
                        <Tile
                            label="Draft + retired questions"
                            value={`${kpis.total_draft_questions} / ${kpis.total_retired_questions}`}
                        />
                        <Tile
                            label="Ontology classes populated"
                            value={`${kpis.ontology_classes_populated} / ${kpis.ontology_classes_total}`}
                            tone={
                                kpis.ontology_classes_populated >=
                                kpis.ontology_classes_total
                                    ? 'good'
                                    : 'neutral'
                            }
                        />
                        <Tile
                            label="Recent runs (30 d)"
                            value={String(kpis.recent_runs_count_30d)}
                        />
                    </section>

                    {/* Questions by set */}
                    <section className="mb-6 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Golden questions by set
                        </h2>
                        <div className="overflow-x-auto">
                            <table
                                className="w-full text-left text-sm"
                                data-testid="questions-by-set"
                            >
                                <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                    <tr>
                                        <th className="px-3 py-2">Question set</th>
                                        <th className="px-3 py-2 text-right">Active</th>
                                        <th className="px-3 py-2 text-right">Draft</th>
                                        <th className="px-3 py-2 text-right">Retired</th>
                                        <th className="px-3 py-2 text-right">Total</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {questions_by_set.length === 0 && (
                                        <tr>
                                            <td
                                                colSpan={5}
                                                className="px-3 py-8 text-center text-stone-500"
                                            >
                                                No golden questions seeded yet. Run{' '}
                                                <code className="text-stone-300">
                                                    python -m app.services.eval.mechanical_questions --commit
                                                </code>
                                                .
                                            </td>
                                        </tr>
                                    )}
                                    {questions_by_set.map((q) => (
                                        <tr
                                            key={q.question_set}
                                            className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30"
                                        >
                                            <td className="px-3 py-2 font-mono text-xs text-stone-200">
                                                {q.question_set}
                                            </td>
                                            <td className="px-3 py-2 text-right text-emerald-300">
                                                {q.active}
                                            </td>
                                            <td className="px-3 py-2 text-right text-amber-300">
                                                {q.draft}
                                            </td>
                                            <td className="px-3 py-2 text-right text-stone-500">
                                                {q.retired}
                                            </td>
                                            <td className="px-3 py-2 text-right">
                                                {q.total}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </section>

                    {/* Difficulty breakdown — small inline */}
                    <section className="mb-6 grid grid-cols-1 gap-6 md:grid-cols-2">
                        <div className="rounded border border-stone-800 bg-stone-900">
                            <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                                Active questions by difficulty
                            </h2>
                            <ul className="divide-y divide-stone-800/60 text-sm">
                                {questions_by_difficulty.length === 0 && (
                                    <li className="px-3 py-6 text-center text-stone-500">
                                        No active questions.
                                    </li>
                                )}
                                {questions_by_difficulty.map((d) => (
                                    <li
                                        key={d.difficulty}
                                        className="flex items-center justify-between px-3 py-2"
                                    >
                                        <span className="font-mono text-xs text-stone-200">
                                            {d.difficulty}
                                        </span>
                                        <span className="rounded bg-stone-800/60 px-2 py-0.5 text-xs text-stone-300">
                                            {d.count}
                                        </span>
                                    </li>
                                ))}
                            </ul>
                        </div>

                        <div className="rounded border border-stone-800 bg-stone-900">
                            <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                                Ontology terms / synonyms
                            </h2>
                            <div className="px-3 py-3 text-sm text-stone-300">
                                <div className="flex justify-between">
                                    <span>Total terms</span>
                                    <span className="font-mono">
                                        {kpis.total_ontology_terms}
                                    </span>
                                </div>
                                <div className="mt-1 flex justify-between">
                                    <span>Total synonyms</span>
                                    <span className="font-mono">
                                        {kpis.total_ontology_synonyms}
                                    </span>
                                </div>
                                <div className="mt-3 text-xs text-stone-500">
                                    §9.3 SME pass populates the remaining{' '}
                                    {kpis.ontology_classes_total -
                                        kpis.ontology_classes_populated}{' '}
                                    class(es) — see the per-class table below.
                                </div>
                            </div>
                        </div>
                    </section>

                    {/* Ontology per-class progress */}
                    <section className="mb-6 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Ontology population progress (§20.1 — 12 classes)
                        </h2>
                        <div className="overflow-x-auto">
                            <table
                                className="w-full text-left text-sm"
                                data-testid="ontology-progress"
                            >
                                <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                    <tr>
                                        <th className="px-3 py-2">Class</th>
                                        <th className="px-3 py-2 text-right">Terms</th>
                                        <th className="px-3 py-2 text-right">Synonyms</th>
                                        <th className="px-3 py-2 text-right">Floor</th>
                                        <th className="px-3 py-2">Status</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {ontology_progress.map((o) => (
                                        <tr
                                            key={o.ontology_class}
                                            className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30"
                                        >
                                            <td className="px-3 py-2 font-mono text-xs text-stone-200">
                                                {o.ontology_class}
                                            </td>
                                            <td className="px-3 py-2 text-right">
                                                <span
                                                    className={
                                                        o.term_count >= o.threshold
                                                            ? 'text-emerald-300'
                                                            : o.term_count > 0
                                                            ? 'text-amber-300'
                                                            : 'text-stone-500'
                                                    }
                                                >
                                                    {o.term_count}
                                                </span>
                                            </td>
                                            <td className="px-3 py-2 text-right text-stone-400">
                                                {o.synonym_count}
                                            </td>
                                            <td className="px-3 py-2 text-right text-stone-500">
                                                {o.threshold}
                                            </td>
                                            <td className="px-3 py-2">
                                                {statusBadge(o.status)}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </section>

                    {/* §04i failure-layer breakdown — doc-phase 171 */}
                    <section
                        className="mb-6 rounded border border-stone-800 bg-stone-900"
                        data-testid="failure-layer-breakdown"
                    >
                        <h2 className="flex items-center justify-between border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            <span>§04i failure-layer breakdown (last 30 days)</span>
                            <span className="text-xs font-normal text-stone-400">
                                {totalLayerFails === 0
                                    ? 'no validator failures'
                                    : `${totalLayerFails} failure${totalLayerFails === 1 ? '' : 's'} across ${
                                          failure_layer_breakdown.filter(
                                              (b) => b.fail_count > 0,
                                          ).length
                                      } layer${
                                          failure_layer_breakdown.filter(
                                              (b) => b.fail_count > 0,
                                          ).length === 1
                                              ? ''
                                              : 's'
                                      }`}
                            </span>
                        </h2>
                        <div className="p-4">
                            <p className="mb-3 text-xs text-stone-400">
                                Per-validator fail counts across all eval runs in
                                the window. Each row maps to one §04i layer; bar
                                width is normalized to the peak count so the
                                hottest layer reads first. When the nightly{' '}
                                <code className="text-stone-300">
                                    eval_real_rag_nightly
                                </code>{' '}
                                cron fires green for a week, all layer rows show
                                a muted "—".
                            </p>
                            <div className="space-y-1.5">
                                {failure_layer_breakdown.map((b) => {
                                    const meta = layerMeta(b.failure_layer);
                                    const pct =
                                        peakLayerFails > 0
                                            ? (b.fail_count / peakLayerFails) * 100
                                            : 0;
                                    const toneClass: Record<string, string> = {
                                        sky:
                                            b.fail_count > 0
                                                ? 'bg-sky-500/70'
                                                : 'bg-stone-700',
                                        stone: 'bg-stone-700',
                                        amber:
                                            b.fail_count > 0
                                                ? 'bg-amber-500/70'
                                                : 'bg-stone-700',
                                    };
                                    const textTone: Record<string, string> = {
                                        sky:
                                            b.fail_count > 0
                                                ? 'text-sky-200'
                                                : 'text-stone-500',
                                        stone: 'text-stone-500',
                                        amber:
                                            b.fail_count > 0
                                                ? 'text-amber-200'
                                                : 'text-stone-500',
                                    };
                                    return (
                                        <div
                                            key={b.failure_layer}
                                            className="grid grid-cols-[260px_1fr_60px_140px] items-center gap-3 text-xs"
                                            data-testid={`layer-row-${b.failure_layer}`}
                                        >
                                            <div className={`font-mono ${textTone[meta.tone]}`}>
                                                {meta.label}
                                            </div>
                                            <div className="h-2 overflow-hidden rounded bg-stone-800">
                                                <div
                                                    className={`h-full ${toneClass[meta.tone]}`}
                                                    style={{ width: `${pct}%` }}
                                                />
                                            </div>
                                            <div
                                                className={`text-right tabular-nums ${
                                                    b.fail_count > 0
                                                        ? 'text-stone-100'
                                                        : 'text-stone-500'
                                                }`}
                                            >
                                                {b.fail_count > 0
                                                    ? b.fail_count
                                                    : '—'}
                                            </div>
                                            <div className="text-right text-stone-500">
                                                {b.last_failed_at
                                                    ? formatDate(b.last_failed_at)
                                                    : '—'}
                                            </div>
                                        </div>
                                    );
                                })}
                            </div>
                        </div>
                    </section>

                    {/* Recent runs */}
                    <section className="mb-6 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Recent eval runs (last 30 days)
                        </h2>
                        <div className="overflow-x-auto">
                            <table
                                className="w-full text-left text-sm"
                                data-testid="recent-runs"
                            >
                                <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                    <tr>
                                        <th className="px-3 py-2">Run ID</th>
                                        <th className="px-3 py-2">Evaluator</th>
                                        <th className="px-3 py-2">Triggered by</th>
                                        <th className="px-3 py-2">Set filter</th>
                                        <th className="px-3 py-2 text-right">Pass</th>
                                        <th className="px-3 py-2 text-right">Fail</th>
                                        <th className="px-3 py-2 text-right">Regr.</th>
                                        <th className="px-3 py-2">Blocks?</th>
                                        <th className="px-3 py-2">Started</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {recent_runs.length === 0 && (
                                        <tr>
                                            <td
                                                colSpan={9}
                                                className="px-3 py-8 text-center text-stone-500"
                                            >
                                                No eval runs in the last 30 days. The §10.4{' '}
                                                <code className="text-stone-300">
                                                    evaluate_workspace
                                                </code>{' '}
                                                Hatchet workflow populates this surface once
                                                its task body graduates from skeleton.
                                            </td>
                                        </tr>
                                    )}
                                    {recent_runs.map((r) => (
                                        <tr
                                            key={r.run_id}
                                            className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30"
                                        >
                                            <td className="px-3 py-2 font-mono text-xs text-stone-300">
                                                {r.run_id.slice(0, 8)}…
                                            </td>
                                            <td className="px-3 py-2 text-xs">
                                                <span
                                                    className={`rounded border px-2 py-0.5 font-mono text-[10px] ${
                                                        r.evaluator_kind === 'real_rag_v1'
                                                            ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300'
                                                            : r.evaluator_kind === 'real_llm_v1'
                                                            ? 'border-sky-500/40 bg-sky-500/10 text-sky-300'
                                                            : 'border-stone-700 bg-stone-800/40 text-stone-400'
                                                    }`}
                                                    title={r.evaluator_kind}
                                                >
                                                    {r.evaluator_kind}
                                                </span>
                                            </td>
                                            <td className="px-3 py-2 text-xs text-stone-400">
                                                {r.triggered_by}
                                            </td>
                                            <td className="px-3 py-2 text-xs text-stone-400">
                                                {r.question_set_filter ?? '*'}
                                            </td>
                                            <td className="px-3 py-2 text-right text-emerald-300">
                                                {r.pass_count}
                                            </td>
                                            <td className="px-3 py-2 text-right text-red-300">
                                                {r.fail_count}
                                            </td>
                                            <td className="px-3 py-2 text-right text-amber-300">
                                                {r.regression_count}
                                            </td>
                                            <td className="px-3 py-2 text-xs">
                                                {r.blocks_promotion ? (
                                                    <span className="rounded border border-red-500/40 bg-red-500/15 px-2 py-0.5 text-red-300">
                                                        blocked
                                                    </span>
                                                ) : (
                                                    <span className="text-stone-500">
                                                        —
                                                    </span>
                                                )}
                                            </td>
                                            <td className="px-3 py-2 text-xs text-stone-400">
                                                {formatDate(r.started_at)}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </section>

                    <footer className="mt-8 text-xs text-stone-500">
                        Read-only. Source tables:{' '}
                        <code className="text-stone-400">
                            eval.golden_questions
                        </code>
                        ,{' '}
                        <code className="text-stone-400">
                            eval.run_summaries
                        </code>
                        ,{' '}
                        <code className="text-stone-400">eval.run_results</code>
                        ,{' '}
                        <code className="text-stone-400">
                            silver.geological_ontology_terms
                        </code>
                        . Last loaded: {new Date().toLocaleString()}.
                    </footer>
                </div>
            </div>
        </AppLayout>
    );
}

function Tile({
    label,
    value,
    tone = 'neutral',
}: {
    label: string;
    value: string;
    tone?: 'good' | 'bad' | 'neutral';
}): JSX.Element {
    const tones: Record<string, string> = {
        good: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300',
        bad: 'border-red-500/40 bg-red-500/10 text-red-300',
        neutral: 'border-stone-800 bg-stone-900 text-stone-100',
    };
    return (
        <div className={`rounded border p-3 ${tones[tone] ?? tones.neutral}`}>
            <div className="text-xs uppercase tracking-wide opacity-80">{label}</div>
            <div className="mt-1 text-2xl font-semibold">{value}</div>
        </div>
    );
}
