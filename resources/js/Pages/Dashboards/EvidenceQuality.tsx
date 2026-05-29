import type { JSX } from 'react';
import { lazy, Suspense } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';
import { StatCard, SectionCard, DataTable, EmptyState } from './_shared';

const Plot = lazy(() => import('react-plotly.js'));

interface PageProps {
    window_days: number;
    totals: { n_answers: number; n_resolved: number; n_rejected: number } | null;
    by_day: { d: string; n_answers: number; n_resolved: number; n_rejected: number; n_refusals: number }[];
    rejection_reasons: { reason: string; n: number }[];
}

export default function EvidenceQuality({ window_days, totals, by_day, rejection_reasons }: PageProps): JSX.Element {
    // Phase 6 — reads from silver.answer_runs rejection stats. audit_ledger_verify
    // broadcasts to admin.dashboards-evidence-quality on every periodic verify run.
    useAdminSurfaceUpdated('dashboards-evidence-quality', null, () => {
        router.reload({ only: ['totals', 'by_day', 'rejection_reasons'] });
    });

    const t = totals ?? { n_answers: 0, n_resolved: 0, n_rejected: 0 };
    const resolvedPct = t.n_answers > 0 ? Math.round(t.n_resolved / t.n_answers * 100) : 0;
    const rejectedPct = t.n_answers > 0 ? Math.round(t.n_rejected / t.n_answers * 100) : 0;

    const trendData = [
        {
            x: by_day.map((d) => d.d),
            y: by_day.map((d) => d.n_resolved),
            type: 'scatter' as const, mode: 'lines+markers' as const,
            name: 'Resolved', line: { color: '#10b981' },
        },
        {
            x: by_day.map((d) => d.d),
            y: by_day.map((d) => d.n_rejected),
            type: 'scatter' as const, mode: 'lines+markers' as const,
            name: 'Rejected', line: { color: '#ef4444' },
        },
        {
            x: by_day.map((d) => d.d),
            y: by_day.map((d) => d.n_refusals),
            type: 'scatter' as const, mode: 'lines+markers' as const,
            name: 'Refusals', line: { color: '#f59e0b' },
        },
    ];

    return (
        <AppLayout>
            <Head title="Evidence Quality" />
            <div className="mx-auto max-w-7xl space-y-6 px-4 py-6 sm:px-6 lg:px-8">
                <div>
                    <h1 className="text-2xl font-semibold text-zinc-900">Evidence Quality</h1>
                    <p className="mt-1 text-sm text-zinc-500">§16.1 · last {window_days} days · citation resolution + refusal trends</p>
                </div>

                <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                    <StatCard label="Total answers" value={t.n_answers.toLocaleString()} sub={`last ${window_days}d`} />
                    <StatCard label="Citation resolved" value={`${resolvedPct}%`} sub={`${t.n_resolved.toLocaleString()} of ${t.n_answers.toLocaleString()}`} />
                    <StatCard label="Citation rejected" value={`${rejectedPct}%`} sub={`${t.n_rejected.toLocaleString()} rejected`} />
                </div>

                <SectionCard title="Daily trend" sub="Answer outcomes per day">
                    {by_day.length === 0 ? (
                        <EmptyState message="No answer runs in this window." />
                    ) : (
                        <Suspense fallback={<div className="h-64" />}>
                            {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
                            <Plot data={trendData as any} layout={{ autosize: true, height: 320, margin: { l: 50, r: 10, t: 10, b: 50 } }} style={{ width: '100%' }} useResizeHandler />
                        </Suspense>
                    )}
                </SectionCard>

                <SectionCard title="Top rejection reasons" sub="Why did citations fail validation?">
                    <DataTable
                        rows={rejection_reasons}
                        columns={[
                            { key: 'reason', label: 'Reason' },
                            { key: 'n', label: 'Count' },
                        ]}
                    />
                </SectionCard>
            </div>
        </AppLayout>
    );
}
