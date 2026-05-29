import type { JSX } from 'react';
import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import { Head, Link } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';

/**
 * /admin/eval/compare — Master-plan §10-v2 (doc-phase 179)
 * Eval Compare dashboard — trend line + side-by-side diff bars +
 * regression drill-down.
 *
 * Backend: app/Http/Controllers/Admin/EvalCompareController.php
 * which proxies FastAPI /api/v1/admin/eval/{runs,assess-promotion}.
 *
 * The promotion-gate enforcer (§10.6) is the verdict action behind
 * the "Assess promotion" button.
 */

const Plot = lazy(() => import('react-plotly.js'));

interface RunRow {
    run_id: string;
    triggered_by: string;
    question_set_filter: string | null;
    started_at: string;
    completed_at: string | null;
    question_count: number;
    pass_count: number;
    fail_count: number;
    regression_count: number;
    blocks_promotion: boolean;
}

interface PageProps {
    recent_runs: RunRow[];
    workspace_id: string;
}

interface PerSetRow {
    question_set: string;
    pass_count: number;
    fail_count: number;
    total_count: number;
    pass_rate_pct: number;
}

interface PerSetSummary {
    run_id: string;
    per_set: PerSetRow[];
}

interface SetDelta {
    question_set: string;
    baseline_count: number;
    candidate_count: number;
    baseline_pass_pct: number;
    candidate_pass_pct: number;
    delta_pct: number;
    is_blocking: boolean;
}

interface Regression {
    question_id: string;
    question_set: string;
    baseline_pass: boolean;
    candidate_pass: boolean;
}

interface AssessResult {
    allow: boolean;
    workspace_id: string;
    candidate_run_id: string;
    baseline_run_id: string;
    regression_threshold_pct: number;
    blocking_sets: string[];
    set_deltas: SetDelta[];
    regressions: Regression[];
}

function shortRun(r: RunRow): string {
    const date = new Date(r.started_at).toISOString().replace('T', ' ').slice(0, 16);
    const set = r.question_set_filter ?? 'all';
    return `${date}  ·  ${set}  ·  ${r.pass_count}/${r.question_count} pass`;
}

function passRate(r: RunRow): number {
    return r.question_count > 0
        ? Math.round((r.pass_count / r.question_count) * 10000) / 100
        : 0;
}

function PlotPlaceholder({ label }: { label: string }): JSX.Element {
    return (
        <div className="flex h-64 items-center justify-center rounded border border-dashed border-zinc-300 text-sm text-zinc-400">
            Loading {label}…
        </div>
    );
}

export default function EvalCompare({ recent_runs, workspace_id }: PageProps): JSX.Element {
    // Default: latest two runs as candidate vs baseline (candidate = newer)
    const [baselineId, setBaselineId] = useState<string>(recent_runs[1]?.run_id ?? '');
    const [candidateId, setCandidateId] = useState<string>(recent_runs[0]?.run_id ?? '');
    const [baselineSummary, setBaselineSummary] = useState<PerSetSummary | null>(null);
    const [candidateSummary, setCandidateSummary] = useState<PerSetSummary | null>(null);
    const [assess, setAssess] = useState<AssessResult | null>(null);
    const [assessLoading, setAssessLoading] = useState(false);

    useEffect(() => {
        if (!baselineId) return;
        fetch(route('admin.eval.compare.per-set', { id: baselineId }), {
            headers: { Accept: 'application/json' },
        })
            .then((r) => r.json())
            .then(setBaselineSummary);
    }, [baselineId]);

    useEffect(() => {
        if (!candidateId) return;
        fetch(route('admin.eval.compare.per-set', { id: candidateId }), {
            headers: { Accept: 'application/json' },
        })
            .then((r) => r.json())
            .then(setCandidateSummary);
    }, [candidateId]);

    // Trend chart: per question_set_filter, points over time
    const trendData = useMemo(() => {
        const bySet: Record<string, { x: string[]; y: number[]; runs: string[] }> = {};
        // Sort ASC for the line chart
        [...recent_runs].reverse().forEach((r) => {
            const key = r.question_set_filter ?? 'all';
            if (!bySet[key]) bySet[key] = { x: [], y: [], runs: [] };
            bySet[key].x.push(r.started_at);
            bySet[key].y.push(passRate(r));
            bySet[key].runs.push(r.run_id);
        });
        return Object.entries(bySet).map(([name, d]) => ({
            x: d.x,
            y: d.y,
            type: 'scatter' as const,
            mode: 'lines+markers' as const,
            name,
            text: d.runs,
            hovertemplate: '%{x}<br>%{y:.1f}%% pass<br>run %{text}<extra>%{fullData.name}</extra>',
        }));
    }, [recent_runs]);

    // Side-by-side compare bars
    const compareData = useMemo(() => {
        if (!baselineSummary || !candidateSummary) return [];
        const sets = Array.from(new Set([
            ...baselineSummary.per_set.map((s) => s.question_set),
            ...candidateSummary.per_set.map((s) => s.question_set),
        ])).sort();
        const baseLookup = Object.fromEntries(baselineSummary.per_set.map((s) => [s.question_set, s.pass_rate_pct]));
        const candLookup = Object.fromEntries(candidateSummary.per_set.map((s) => [s.question_set, s.pass_rate_pct]));
        return [
            {
                x: sets, y: sets.map((s) => baseLookup[s] ?? 0),
                type: 'bar' as const, name: 'Baseline',
                marker: { color: '#94a3b8' },
            },
            {
                x: sets, y: sets.map((s) => candLookup[s] ?? 0),
                type: 'bar' as const, name: 'Candidate',
                marker: { color: '#6366f1' },
            },
        ];
    }, [baselineSummary, candidateSummary]);

    function runAssess() {
        if (!baselineId || !candidateId || baselineId === candidateId) {
            alert('Pick two distinct runs first.');
            return;
        }
        setAssessLoading(true);
        setAssess(null);
        fetch(route('admin.eval.compare.assess'), {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-TOKEN': (document.querySelector('meta[name="csrf-token"]') as HTMLMetaElement)?.content ?? '',
                Accept: 'application/json',
            },
            body: JSON.stringify({
                workspace_id: workspace_id,
                candidate_run_id: candidateId,
                baseline_run_id: baselineId,
                dry_run: true,  // Don't pollute audit chain from the UI
            }),
        })
            .then(async (r) => {
                if (!r.ok) throw new Error(await r.text());
                setAssess(await r.json());
            })
            .catch((err) => alert(`Assess failed: ${err.message}`))
            .finally(() => setAssessLoading(false));
    }

    return (
        <AppLayout>
            <Head title="Eval Compare" />

            <div className="mx-auto max-w-7xl space-y-6 px-4 py-6 sm:px-6 lg:px-8">
                <div className="flex items-center justify-between">
                    <div>
                        <h1 className="text-2xl font-semibold text-zinc-900">Eval Compare</h1>
                        <p className="mt-1 text-sm text-zinc-500">
                            §10-v2 · trend + side-by-side diff · {recent_runs.length} runs in last 30d
                        </p>
                    </div>
                    <Link
                        href={route('admin.eval.questions.index')}
                        className="text-sm text-indigo-600 hover:underline"
                    >
                        Manage questions →
                    </Link>
                </div>

                {/* Trend chart */}
                <section className="rounded-lg border border-zinc-200 bg-white p-4">
                    <h2 className="text-sm font-medium text-zinc-700">Pass-rate trend (last 30d)</h2>
                    <p className="text-xs text-zinc-500">One line per question_set_filter — hover any point for run ID + counts.</p>
                    <div className="mt-3">
                        {trendData.length === 0 ? (
                            <div className="rounded border border-dashed border-zinc-300 p-6 text-center text-sm text-zinc-500">
                                No runs in the last 30 days.
                            </div>
                        ) : (
                            <Suspense fallback={<PlotPlaceholder label="trend chart" />}>
                                <Plot
                                    data={trendData as any}
                                    layout={{
                                        autosize: true, height: 320,
                                        margin: { l: 50, r: 10, t: 10, b: 50 },
                                        xaxis: { title: { text: 'Run started' }, type: 'date' },
                                        yaxis: { title: { text: 'Pass rate %' }, range: [0, 100] },
                                        legend: { orientation: 'h' },
                                    }}
                                    style={{ width: '100%' }}
                                    useResizeHandler
                                />
                            </Suspense>
                        )}
                    </div>
                </section>

                {/* Compare picker */}
                <section className="rounded-lg border border-zinc-200 bg-white p-4">
                    <h2 className="text-sm font-medium text-zinc-700">Compare two runs</h2>
                    <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
                        <div>
                            <label className="block text-xs font-medium text-zinc-600">Baseline</label>
                            <select
                                className="mt-1 block w-full rounded-md border-zinc-300 text-xs"
                                value={baselineId}
                                onChange={(e) => setBaselineId(e.target.value)}
                            >
                                <option value="">(pick a run)</option>
                                {recent_runs.map((r) => (
                                    <option key={r.run_id} value={r.run_id}>{shortRun(r)}</option>
                                ))}
                            </select>
                        </div>
                        <div>
                            <label className="block text-xs font-medium text-zinc-600">Candidate</label>
                            <select
                                className="mt-1 block w-full rounded-md border-zinc-300 text-xs"
                                value={candidateId}
                                onChange={(e) => setCandidateId(e.target.value)}
                            >
                                <option value="">(pick a run)</option>
                                {recent_runs.map((r) => (
                                    <option key={r.run_id} value={r.run_id}>{shortRun(r)}</option>
                                ))}
                            </select>
                        </div>
                    </div>

                    {/* Side-by-side bars */}
                    {baselineSummary && candidateSummary && (
                        <div className="mt-4">
                            <Suspense fallback={<PlotPlaceholder label="compare bars" />}>
                                <Plot
                                    data={compareData as any}
                                    layout={{
                                        barmode: 'group', autosize: true, height: 320,
                                        margin: { l: 50, r: 10, t: 10, b: 80 },
                                        xaxis: { title: { text: 'question_set' }, tickangle: -30 },
                                        yaxis: { title: { text: 'Pass rate %' }, range: [0, 100] },
                                        legend: { orientation: 'h' },
                                    }}
                                    style={{ width: '100%' }}
                                    useResizeHandler
                                />
                            </Suspense>
                        </div>
                    )}

                    {/* Verdict */}
                    <div className="mt-4 flex items-center gap-3">
                        <button
                            type="button"
                            onClick={runAssess}
                            disabled={assessLoading || !baselineId || !candidateId || baselineId === candidateId}
                            className="rounded bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
                        >
                            {assessLoading ? 'Assessing…' : 'Assess promotion'}
                        </button>
                        {assess && (
                            <div className={`rounded px-3 py-2 text-sm font-medium ${assess.allow ? 'bg-emerald-50 text-emerald-700' : 'bg-red-50 text-red-700'}`}>
                                {assess.allow ? '✓ Promotion ALLOWED' : `✗ Promotion BLOCKED — regressed sets: ${assess.blocking_sets.join(', ')}`}
                                <span className="ml-2 text-xs font-normal text-zinc-500">
                                    threshold: {assess.regression_threshold_pct}pp
                                </span>
                            </div>
                        )}
                    </div>
                </section>

                {/* Drill-down */}
                {assess && assess.regressions.length > 0 && (
                    <section className="rounded-lg border border-zinc-200 bg-white p-4">
                        <h2 className="text-sm font-medium text-zinc-700">
                            Regressions ({assess.regressions.length} questions: was-pass → now-fail)
                        </h2>
                        <table className="mt-3 min-w-full divide-y divide-zinc-200 text-sm">
                            <thead className="bg-zinc-50">
                                <tr>
                                    <th className="px-3 py-2 text-left font-medium text-zinc-700">Question ID</th>
                                    <th className="px-3 py-2 text-left font-medium text-zinc-700">Set</th>
                                    <th className="px-3 py-2 text-left font-medium text-zinc-700">Open</th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-zinc-100">
                                {assess.regressions.map((r) => (
                                    <tr key={r.question_id}>
                                        <td className="px-3 py-2 font-mono text-xs">{r.question_id.slice(0, 8)}…</td>
                                        <td className="px-3 py-2 text-zinc-600">{r.question_set}</td>
                                        <td className="px-3 py-2">
                                            <Link
                                                href={route('admin.eval.questions.show', { id: r.question_id })}
                                                className="text-indigo-600 hover:underline"
                                            >
                                                View question →
                                            </Link>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </section>
                )}
            </div>
        </AppLayout>
    );
}
