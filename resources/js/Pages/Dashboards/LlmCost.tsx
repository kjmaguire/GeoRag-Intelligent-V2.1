import type { JSX } from 'react';
import { lazy, Suspense } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';
import { StatCard, SectionCard, DataTable, EmptyState } from './_shared';

const Plot = lazy(() => import('react-plotly.js'));

interface PageProps {
    window_days: number;
    totals: { invocations: number; total_tokens: number; cost_usd: number } | null;
    by_day: { rollup_date: string; invocations: number; prompt_tokens: number; completion_tokens: number; cost_usd: number }[];
    by_agent: { agent_name: string; invocations: number; total_tokens: number; cost_usd: number }[];
}

export default function LlmCost({ window_days, totals, by_day, by_agent }: PageProps): JSX.Element {
    const t = totals ?? { invocations: 0, total_tokens: 0, cost_usd: 0 };
    const avgPerInvoke = t.invocations > 0 ? (Number(t.cost_usd) / t.invocations).toFixed(4) : '0';

    // Phase 3 real-time push — cost_burn_watcher broadcasts to admin.llm-cost
    // every run (regardless of whether an alert was emitted). Reuses the
    // Phase 2 AdminSurfaceUpdated infrastructure: LlmCost is operationally
    // admin-only (shows global usage.usage_aggregates_daily) so the admin
    // channel is the natural target.
    useAdminSurfaceUpdated('llm-cost', null, () => {
        router.reload({ only: ['totals', 'by_day', 'by_agent'] });
    });

    const costTrend = [{
        x: by_day.map((d) => d.rollup_date),
        y: by_day.map((d) => Number(d.cost_usd)),
        type: 'bar' as const,
        name: 'Daily cost (USD)',
        marker: { color: '#6366f1' },
    }];

    return (
        <AppLayout>
            <Head title="LLM Cost & Usage" />
            <div className="mx-auto max-w-7xl space-y-6 px-4 py-6 sm:px-6 lg:px-8">
                <div>
                    <h1 className="text-2xl font-semibold text-zinc-900">LLM Cost & Usage</h1>
                    <p className="mt-1 text-sm text-zinc-500">§16.1 · last {window_days} days · token spend per agent</p>
                </div>

                <div className="grid grid-cols-1 gap-4 sm:grid-cols-4">
                    <StatCard label="Invocations" value={t.invocations.toLocaleString()} sub={`last ${window_days}d`} />
                    <StatCard label="Total tokens" value={Number(t.total_tokens).toLocaleString()} />
                    <StatCard label="Total cost (USD)" value={`$${Number(t.cost_usd).toFixed(4)}`} />
                    <StatCard label="Avg per call" value={`$${avgPerInvoke}`} />
                </div>

                <SectionCard title="Daily cost trend" sub="from usage.usage_aggregates_daily">
                    {by_day.length === 0 ? (
                        <EmptyState message="No usage rolled up yet — emit some llm_call events to populate." />
                    ) : (
                        <Suspense fallback={<div className="h-64" />}>
                            {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
                            <Plot data={costTrend as any} layout={{ autosize: true, height: 280, margin: { l: 50, r: 10, t: 10, b: 50 } }} style={{ width: '100%' }} useResizeHandler />
                        </Suspense>
                    )}
                </SectionCard>

                <SectionCard title="Top agents by spend">
                    <DataTable
                        rows={by_agent}
                        columns={[
                            { key: 'agent_name', label: 'Agent' },
                            { key: 'invocations', label: 'Invocations' },
                            { key: 'total_tokens', label: 'Tokens', render: (v) => Number(v).toLocaleString() },
                            { key: 'cost_usd', label: 'Cost (USD)', render: (v) => `$${Number(v).toFixed(4)}` },
                        ]}
                    />
                </SectionCard>
            </div>
        </AppLayout>
    );
}
