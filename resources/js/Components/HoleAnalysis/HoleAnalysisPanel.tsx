import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import Stereonet from './Stereonet';  // pure SVG, no Plotly — safe to eagerly import

// Plotly-based children are lazy-loaded to match the pattern used by
// InlineViz. Without this, rolldown's CJS-interop for react-plotly.js
// resolves `createPlotlyComponent` to `undefined` at the top-level
// Explorer load and the Analysis tab crashes with
// "(0, wk.default) is not a function".
const OrientationSpiral = lazy(() => import('./OrientationSpiral'));
const AzimuthDipVsDepth = lazy(() => import('./AzimuthDipVsDepth'));
const GeochemPlots      = lazy(() => import('./GeochemPlots'));
const Stereosphere      = lazy(() => import('./Stereosphere'));

type ViewMode = '2d' | '3d';

/**
 * Tiny pill-style segmented control used in the top-right of each
 * sub-tab that supports dual-dimension rendering.
 */
function ViewModeToggle({ value, onChange }: { value: ViewMode; onChange: (v: ViewMode) => void }) {
    return (
        <div
            role="group"
            aria-label="View dimension"
            className="inline-flex rounded-full border border-gray-700 bg-gray-900/60 p-0.5 text-[11px] font-medium"
        >
            {(['2d', '3d'] as const).map((m) => {
                const active = value === m;
                return (
                    <button
                        key={m}
                        type="button"
                        onClick={() => onChange(m)}
                        aria-pressed={active}
                        className={`px-2.5 py-0.5 rounded-full transition-colors ${
                            active
                                ? 'bg-amber-400 text-gray-950'
                                : 'text-gray-400 hover:text-gray-200'
                        }`}
                    >
                        {m === '2d' ? '2D' : '3D'}
                    </button>
                );
            })}
        </div>
    );
}

const Loader = () => (
    <div className="flex items-center justify-center h-full text-xs text-gray-500">
        Loading chart…
    </div>
);

interface AnalysisCollar {
    collar_id: string;
    hole_id: string;
    hole_type: string | null;
    status: string | null;
    total_depth: number | null;
    azimuth: number | null;
    dip: number | null;
    elevation: number | null;
    easting: number | null;
    northing: number | null;
}

interface Survey {
    depth: number;
    azimuth: number | null;
    dip: number | null;
    survey_method?: string | null;
}

interface Structure {
    depth: number;
    structure_type: string;
    alpha_angle: number | null;
    beta_angle: number | null;
    true_dip: number | null;
    dip_direction: number | null;
    description?: string | null;
}

interface GeochemRow {
    from_depth: number;
    to_depth: number;
    sio2_wt_pct: number | null;
    al2o3_wt_pct: number | null;
    fe2o3_wt_pct: number | null;
    mgo_wt_pct: number | null;
    mg_number: number | null;
    cia: number | null;
    eu_anomaly: number | null;
    ree_json: unknown;
}

interface AnalysisPayload {
    collar: AnalysisCollar;
    surveys: Survey[];
    structures: Structure[];
    geochem: GeochemRow[];
}

interface HoleAnalysisPanelProps {
    holeId: string;
    projectId: string;
}

type SubTab = 'spiral' | 'azimuth' | 'dip' | 'stereonet' | 'geochem';

const SUB_TABS: { id: SubTab; label: string }[] = [
    { id: 'spiral',    label: 'Orientation Spiral' },
    { id: 'azimuth',   label: 'Azimuth vs Depth' },
    { id: 'dip',       label: 'Dip vs Depth' },
    { id: 'stereonet', label: 'Stereonet' },
    { id: 'geochem',   label: 'Geochemistry' },
];

/**
 * Top-level Analysis tab content for a selected drill hole. Fetches the
 * hole's full analysis payload (surveys + structures + geochem) in one
 * request, then routes the user between the five sub-views with an
 * internal tab bar.
 *
 * Handles three unhappy paths:
 *   - loading (skeleton message)
 *   - fetch failure (error banner + Retry button)
 *   - "no data of this kind yet" per-tab (each child renders its own empty state)
 */
export default function HoleAnalysisPanel({ holeId, projectId }: HoleAnalysisPanelProps) {
    const [data, setData] = useState<AnalysisPayload | null>(null);
    const [loading, setLoading] = useState<boolean>(true);
    const [error, setError] = useState<string | null>(null);
    const [subTab, setSubTab] = useState<SubTab>('spiral');

    const fetchAnalysis = useCallback(async () => {
        if (!holeId || !projectId) return;
        setLoading(true);
        setError(null);
        try {
            // Auth via Sanctum session cookie (same-origin). No bearer token from
            // localStorage — localStorage is an XSS-exfiltration target (types.ts:11-12).
            const csrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const res = await fetch(
                `/api/v1/projects/${projectId}/holes/${encodeURIComponent(holeId)}/analysis`,
                {
                    credentials: 'same-origin',
                    headers: {
                        Accept: 'application/json',
                        'X-Requested-With': 'XMLHttpRequest',
                        ...(csrf ? { 'X-CSRF-TOKEN': csrf } : {}),
                    },
                }
            );
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const body = (await res.json()) as AnalysisPayload;
            setData(body);
        } catch (err) {
            setError(err instanceof Error ? err.message : 'Failed to load analysis.');
        } finally {
            setLoading(false);
        }
    }, [holeId, projectId]);

    useEffect(() => {
        fetchAnalysis();
    }, [fetchAnalysis]);

    // View-mode (2D/3D) per sub-tab that supports it. Lifted here so
    // switching between tabs preserves each tab's chosen dimension.
    // Spiral defaults to 3D (its original form); the others default to
    // 2D which is their more immediately-readable representation.
    const [spiralView,  setSpiralView]  = useState<ViewMode>('3d');
    const [azimuthView, setAzimuthView] = useState<ViewMode>('2d');
    const [dipView,     setDipView]     = useState<ViewMode>('2d');
    const [stereoView,  setStereoView]  = useState<ViewMode>('2d');

    // Stereonet visibility toggles are lifted to the panel so the state
    // survives the user switching between sub-tabs.
    const [visibleTypes, setVisibleTypes] = useState<Record<string, boolean>>({});
    useEffect(() => {
        if (data?.structures.length) {
            const types = new Set(data.structures.map((s) => s.structure_type));
            setVisibleTypes((prev) => {
                // Initialise any newly-seen types to visible, preserve user toggles.
                const next = { ...prev };
                types.forEach((t) => {
                    if (next[t] === undefined) next[t] = true;
                });
                return next;
            });
        }
    }, [data?.structures]);

    const structureCounts = useMemo(() => {
        if (!data?.structures) return {} as Record<string, number>;
        const counts: Record<string, number> = {};
        for (const s of data.structures) {
            counts[s.structure_type] = (counts[s.structure_type] || 0) + 1;
        }
        return counts;
    }, [data?.structures]);

    if (loading) {
        return (
            <div className="p-6 text-gray-400">
                Loading analysis for <span className="text-gray-200 font-mono">{holeId}</span>…
            </div>
        );
    }

    if (error) {
        return (
            <div className="p-6 flex flex-col items-start gap-3">
                <div className="text-red-400 text-sm">
                    Failed to load analysis for {holeId}: {error}
                </div>
                <button
                    type="button"
                    onClick={fetchAnalysis}
                    className="px-3 py-1 text-xs border border-gray-700 rounded hover:bg-gray-800"
                >
                    Retry
                </button>
            </div>
        );
    }

    if (!data) return null;

    const { collar, surveys, structures, geochem } = data;

    return (
        <div className="flex flex-col h-full overflow-hidden">
            {/* Header strip — collar summary */}
            <div className="px-4 py-3 border-b border-gray-800 flex flex-wrap gap-x-5 gap-y-1 text-xs text-gray-300">
                <span>
                    <span className="text-gray-500">Hole: </span>
                    <span className="font-mono">{collar.hole_id}</span>
                </span>
                {collar.total_depth != null && (
                    <span>
                        <span className="text-gray-500">TD: </span>
                        {collar.total_depth.toFixed(1)} m
                    </span>
                )}
                {collar.azimuth != null && (
                    <span>
                        <span className="text-gray-500">Az: </span>
                        {collar.azimuth.toFixed(1)}°
                    </span>
                )}
                {collar.dip != null && (
                    <span>
                        <span className="text-gray-500">Dip: </span>
                        {collar.dip.toFixed(1)}°
                    </span>
                )}
                <span className="text-gray-500">
                    {surveys.length} surveys · {structures.length} structures · {geochem.length} geochem intervals
                </span>
            </div>

            {/* Sub-tab nav */}
            <div role="tablist" aria-label="Analysis sub-views" className="flex border-b border-gray-800 px-4 bg-gray-950/40">
                {SUB_TABS.map((t) => {
                    const isActive = subTab === t.id;
                    return (
                        <button
                            key={t.id}
                            role="tab"
                            aria-selected={isActive}
                            aria-controls={`analysis-panel-${t.id}`}
                            id={`analysis-tab-${t.id}`}
                            type="button"
                            onClick={() => setSubTab(t.id)}
                            className={`px-4 py-2 text-sm border-b-2 -mb-px transition-colors ${
                                isActive
                                    ? 'border-amber-400 text-amber-300'
                                    : 'border-transparent text-gray-400 hover:text-gray-200'
                            }`}
                        >
                            {t.label}
                        </button>
                    );
                })}
            </div>

            {/* Panel content */}
            <div className="flex-1 overflow-y-auto p-4">
                {subTab === 'spiral' && (
                    <div
                        id="analysis-panel-spiral"
                        role="tabpanel"
                        aria-labelledby="analysis-tab-spiral"
                    >
                        <div className="flex items-center justify-between mb-2">
                            <div className="text-xs text-gray-500">
                                {spiralView === '3d'
                                    ? 'Rotatable 3-D trajectory — drag to rotate, scroll to zoom'
                                    : 'Plan + Section — NI 43-101 standard report layout'}
                            </div>
                            <ViewModeToggle value={spiralView} onChange={setSpiralView} />
                        </div>
                        <div className={spiralView === '3d' ? 'h-[540px]' : 'h-[420px]'}>
                            <Suspense fallback={<Loader />}>
                                <OrientationSpiral
                                    surveys={surveys}
                                    collarAzimuth={collar.azimuth}
                                    collarDip={collar.dip}
                                    collarElevation={collar.elevation}
                                    totalDepth={collar.total_depth}
                                    view={spiralView}
                                />
                            </Suspense>
                        </div>
                    </div>
                )}

                {subTab === 'azimuth' && (
                    <div
                        id="analysis-panel-azimuth"
                        role="tabpanel"
                        aria-labelledby="analysis-tab-azimuth"
                        className={azimuthView === '3d' ? 'h-[540px] mx-auto' : 'h-[480px] max-w-xl mx-auto'}
                    >
                        <div className="flex items-center justify-between mb-2">
                            <div className="text-xs text-gray-500">
                                {azimuthView === '3d' ? 'Cylindrical helix — drag to rotate' : 'Line chart — depth on Y (reversed)'}
                            </div>
                            <ViewModeToggle value={azimuthView} onChange={setAzimuthView} />
                        </div>
                        <div className={azimuthView === '3d' ? 'h-[500px]' : 'h-[440px]'}>
                            <Suspense fallback={<Loader />}>
                                <AzimuthDipVsDepth
                                    surveys={surveys}
                                    mode="azimuth"
                                    view={azimuthView}
                                    collarValue={collar.azimuth}
                                    collarAzimuth={collar.azimuth}
                                />
                            </Suspense>
                        </div>
                    </div>
                )}

                {subTab === 'dip' && (
                    <div
                        id="analysis-panel-dip"
                        role="tabpanel"
                        aria-labelledby="analysis-tab-dip"
                        className={dipView === '3d' ? 'h-[540px] mx-auto' : 'h-[480px] max-w-xl mx-auto'}
                    >
                        <div className="flex items-center justify-between mb-2">
                            <div className="text-xs text-gray-500">
                                {dipView === '3d' ? 'Slant trajectory — drag to rotate' : 'Line chart — depth on Y (reversed)'}
                            </div>
                            <ViewModeToggle value={dipView} onChange={setDipView} />
                        </div>
                        <div className={dipView === '3d' ? 'h-[500px]' : 'h-[440px]'}>
                            <Suspense fallback={<Loader />}>
                                <AzimuthDipVsDepth
                                    surveys={surveys}
                                    mode="dip"
                                    view={dipView}
                                    collarValue={collar.dip}
                                    collarAzimuth={collar.azimuth}
                                />
                            </Suspense>
                        </div>
                    </div>
                )}

                {subTab === 'stereonet' && (
                    <div
                        id="analysis-panel-stereonet"
                        role="tabpanel"
                        aria-labelledby="analysis-tab-stereonet"
                        className="flex flex-col lg:flex-row gap-4"
                    >
                        <aside className="lg:w-60 shrink-0 space-y-3">
                            <div className="flex items-center justify-between">
                                <div className="text-xs uppercase tracking-wide text-gray-500">Display Options</div>
                                <ViewModeToggle value={stereoView} onChange={setStereoView} />
                            </div>
                            <div className="space-y-1.5">
                                {Object.entries(structureCounts).map(([type, n]) => (
                                    <label key={type} className="flex items-center gap-2 text-xs text-gray-300 cursor-pointer select-none">
                                        <input
                                            type="checkbox"
                                            checked={visibleTypes[type] ?? true}
                                            onChange={(e) =>
                                                setVisibleTypes((prev) => ({ ...prev, [type]: e.target.checked }))
                                            }
                                            className="accent-amber-400"
                                        />
                                        <span className="capitalize">{type}</span>
                                        <span className="text-gray-500">({n})</span>
                                    </label>
                                ))}
                            </div>
                        </aside>
                        <div className="flex-1">
                            {structures.length === 0 ? (
                                <div className="h-[360px] flex items-center justify-center text-gray-500 text-sm">
                                    No structural measurements logged for {holeId}.
                                </div>
                            ) : stereoView === '3d' ? (
                                <Suspense fallback={<Loader />}>
                                    <Stereosphere
                                        structures={structures}
                                        holeId={holeId}
                                        visibleTypes={visibleTypes}
                                    />
                                </Suspense>
                            ) : (
                                <Stereonet
                                    structures={structures}
                                    holeId={holeId}
                                    visibleTypes={visibleTypes}
                                />
                            )}
                        </div>
                    </div>
                )}

                {subTab === 'geochem' && (
                    <div
                        id="analysis-panel-geochem"
                        role="tabpanel"
                        aria-labelledby="analysis-tab-geochem"
                    >
                        <Suspense fallback={<Loader />}>
                            <GeochemPlots rows={geochem as any} holeId={holeId} />
                        </Suspense>
                    </div>
                )}
            </div>
        </div>
    );
}
