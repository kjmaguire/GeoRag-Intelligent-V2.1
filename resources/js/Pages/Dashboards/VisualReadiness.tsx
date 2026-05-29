import type { JSX } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';
import { StatCard, SectionCard } from './_shared';

interface PageProps {
    viz_coverage: { kind: string; ready: number; total: number }[];
    total_projects: number;
}

const CHART_LABELS: Record<string, string> = {
    strip_log: 'Strip log',
    cross_section: 'Cross-section',
    stereonet: 'Stereonet',
    long_section: 'Long section',
    harker_diagram: 'Harker diagram',
    spider_diagram: 'Spider diagram',
    ree_pattern: 'REE pattern',
    ternary_diagram: 'Ternary diagram',
    grade_tonnage: 'Grade-tonnage',
    anomaly_map: 'Anomaly map',
    target_heatmap: 'Target heatmap',
};

export default function VisualReadiness({ viz_coverage, total_projects }: PageProps): JSX.Element {
    // Phase 6 — mv_refresh_silver (via DebounceWorkspaceMvRefresh)
    // broadcasts admin.dashboards-visual-readiness on every successful refresh.
    useAdminSurfaceUpdated('dashboards-visual-readiness', null, () => {
        router.reload({ only: ['viz_coverage', 'total_projects'] });
    });

    const totalReady = viz_coverage.reduce((acc, v) => acc + v.ready, 0);
    const totalCells = viz_coverage.length * total_projects;
    const overallPct = totalCells > 0 ? Math.round(totalReady / totalCells * 100) : 0;

    return (
        <AppLayout>
            <Head title="Visual Readiness" />
            <div className="mx-auto max-w-7xl space-y-6 px-4 py-6 sm:px-6 lg:px-8">
                <div>
                    <h1 className="text-2xl font-semibold text-zinc-900">Visual Readiness</h1>
                    <p className="mt-1 text-sm text-zinc-500">§16.1 · which of the 11 chart types have data per project?</p>
                </div>

                <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                    <StatCard label="Active projects" value={total_projects} />
                    <StatCard label="Chart types" value={viz_coverage.length} />
                    <StatCard label="Coverage" value={`${overallPct}%`} sub={`${totalReady} of ${totalCells} (kind × project) cells`} />
                </div>

                <SectionCard title="Per-chart coverage" sub="ready = projects with enough data to render this chart">
                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                        {viz_coverage.map((v) => {
                            const pct = v.total > 0 ? Math.round(v.ready / v.total * 100) : 0;
                            return (
                                <div key={v.kind} className="rounded-lg border border-zinc-200 bg-white p-3">
                                    <div className="flex items-center justify-between">
                                        <span className="text-sm font-medium">{CHART_LABELS[v.kind] ?? v.kind}</span>
                                        <span className={`text-xs ${pct === 0 ? 'text-zinc-400' : pct < 50 ? 'text-amber-700' : 'text-emerald-700'}`}>
                                            {pct}%
                                        </span>
                                    </div>
                                    <div className="mt-2 h-2 w-full overflow-hidden rounded bg-zinc-100">
                                        <div className={pct === 0 ? 'h-full bg-zinc-300' : pct < 50 ? 'h-full bg-amber-500' : 'h-full bg-emerald-500'} style={{ width: `${pct}%` }} />
                                    </div>
                                    <div className="mt-1 text-xs text-zinc-500">{v.ready} of {v.total} projects</div>
                                </div>
                            );
                        })}
                    </div>
                    <p className="mt-4 text-xs text-zinc-500">
                        Try the <Link href="/charts/gallery" className="text-indigo-600 hover:underline">Charts Gallery</Link> to preview every type against synthetic data.
                    </p>
                </SectionCard>
            </div>
        </AppLayout>
    );
}
