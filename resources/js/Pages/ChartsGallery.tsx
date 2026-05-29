import type { JSX } from 'react';
import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import { Head } from '@inertiajs/react';
import AppLayout from '../Layouts/AppLayout';

/**
 * /charts/gallery — §17.3 Charts Gallery
 *
 * Showcases the 8 new chart types added in §17.3 wave 2 (in addition
 * to the original 3: strip-log, cross-section, stereonet).
 *
 * Each chart renders against a synthetic demo dataset by default —
 * operators see the shape before real data exists. Real-data wiring
 * for each chart is per-project + per-chart and lives in the relevant
 * cockpit / project page.
 */

const Plot = lazy(() => import('react-plotly.js'));

interface PageProps {
    chart_kinds: string[];
}

const CHART_META: Record<string, { label: string; description: string }> = {
    long_section: {
        label: 'Long section',
        description:
            'Drillhole traces projected onto a vertical plane along a reference azimuth — the geologist\'s primary "side view" of a drilling program.',
    },
    harker_diagram: {
        label: 'Harker diagram',
        description:
            'SiO₂ (x) vs another major oxide (y) — classic igneous petrology classification scatter. Color-coded by rock type.',
    },
    spider_diagram: {
        label: 'Spider diagram',
        description:
            'Multi-element pattern normalized to primitive mantle. Reveals magma source signature + crustal contamination.',
    },
    ree_pattern: {
        label: 'REE pattern',
        description:
            'Rare-earth-element pattern normalized to C1 chondrite. La→Lu ordering shows light vs heavy REE enrichment + Eu anomaly.',
    },
    ternary_diagram: {
        label: 'Ternary diagram',
        description:
            '3-component composition triangle (e.g. AFM: Na+K vs FeO vs MgO). Useful for any 3-end-member classification.',
    },
    grade_tonnage: {
        label: 'Grade-tonnage curve',
        description:
            'Dual-axis: cumulative tonnage above cutoff (left) + weighted-average grade above cutoff (right). The classic resource-evaluation summary.',
    },
    anomaly_map: {
        label: 'Anomaly map',
        description:
            'Sample points colored by Z-score of a single element. Points >2σ above mean are flagged as anomalies.',
    },
    target_heatmap: {
        label: 'Target heatmap',
        description:
            'h3 hex-cell aggregate target score across the project AOI. Drop-in for §6.6 gold.h3_density_mineral output.',
    },
};

interface PlotlyFigure {
    data: unknown[];
    layout: Record<string, unknown>;
}

function PlotPlaceholder(): JSX.Element {
    return (
        <div className="flex h-72 items-center justify-center rounded border border-dashed border-zinc-300 text-sm text-zinc-400">
            Loading chart…
        </div>
    );
}

function csrfToken(): string {
    return (document.querySelector('meta[name="csrf-token"]') as HTMLMetaElement)?.content ?? '';
}

export default function ChartsGallery({ chart_kinds }: PageProps): JSX.Element {
    const [figures, setFigures] = useState<Record<string, PlotlyFigure | { error: string } | null>>({});
    const [openKind, setOpenKind] = useState<string | null>(null);

    const renderChart = useCallback(async (kind: string) => {
        setFigures((prev) => ({ ...prev, [kind]: null }));
        try {
            const r = await fetch('/api/v1/charts/render', {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-TOKEN': csrfToken(),
                    Accept: 'application/json',
                },
                body: JSON.stringify({ chart_kind: kind, params: null }),
            });
            if (!r.ok) {
                const err = await r.text();
                setFigures((prev) => ({ ...prev, [kind]: { error: err } }));
                return;
            }
            const fig: PlotlyFigure = await r.json();
            setFigures((prev) => ({ ...prev, [kind]: fig }));
        } catch (e) {
            setFigures((prev) => ({
                ...prev,
                [kind]: { error: e instanceof Error ? e.message : String(e) },
            }));
        }
    }, []);

    useEffect(() => {
        // Auto-load all charts on first paint so the gallery feels alive.
        chart_kinds.forEach((k) => { void renderChart(k); });
    }, [chart_kinds, renderChart]);

    return (
        <AppLayout>
            <Head title="Charts Gallery" />
            <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
                <div className="flex items-end justify-between">
                    <div>
                        <h1 className="text-2xl font-semibold text-zinc-900">Charts Gallery</h1>
                        <p className="mt-1 text-sm text-zinc-500">
                            §17.3 visualization library · {chart_kinds.length} chart kinds · synthetic demo data
                        </p>
                    </div>
                </div>

                <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-2">
                    {chart_kinds.map((kind) => {
                        const fig = figures[kind];
                        const meta = CHART_META[kind] ?? {
                            label: kind,
                            description: '(no description)',
                        };
                        return (
                            <section
                                key={kind}
                                className="rounded-lg border border-zinc-200 bg-white p-4"
                            >
                                <div className="flex items-center justify-between">
                                    <h2 className="text-base font-medium text-zinc-900">
                                        {meta.label}
                                    </h2>
                                    <button
                                        type="button"
                                        onClick={() => renderChart(kind)}
                                        className="rounded border border-zinc-300 px-2 py-0.5 text-xs hover:bg-zinc-50"
                                    >
                                        Re-render
                                    </button>
                                </div>
                                <p className="mt-1 text-xs text-zinc-500">{meta.description}</p>

                                <div className="mt-3">
                                    {fig === undefined || fig === null ? (
                                        <PlotPlaceholder />
                                    ) : 'error' in fig ? (
                                        <div className="rounded border border-red-200 bg-red-50 p-3 text-xs text-red-800">
                                            Failed: {(fig as { error: string }).error}
                                        </div>
                                    ) : (
                                        <Suspense fallback={<PlotPlaceholder />}>
                                            <Plot
                                                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                                                data={fig.data as any}
                                                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                                                layout={fig.layout as any}
                                                style={{ width: '100%' }}
                                                useResizeHandler
                                                config={{ displayModeBar: false, responsive: true }}
                                            />
                                        </Suspense>
                                    )}
                                </div>

                                <button
                                    type="button"
                                    onClick={() => setOpenKind(openKind === kind ? null : kind)}
                                    className="mt-2 text-xs text-indigo-600 hover:underline"
                                >
                                    {openKind === kind ? 'Hide' : 'Show'} API spec
                                </button>
                                {openKind === kind && (
                                    <pre className="mt-2 max-h-48 overflow-auto rounded bg-zinc-50 p-2 text-[10px] text-zinc-700">
{`POST /api/v1/charts/render
{
  "chart_kind": "${kind}",
  "params": {  // ...inputs for this chart type...
    // pass null to use the synthetic demo dataset
  }
}`}
                                    </pre>
                                )}
                            </section>
                        );
                    })}
                </div>
            </div>
        </AppLayout>
    );
}
