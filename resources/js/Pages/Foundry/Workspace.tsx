import { Suspense, lazy, useEffect, useState } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, Segmented, EmptyState } from '@/Components/Foundry/primitives';
import { StereonetMini, RoseMini, DownholeMultiLog, ChronoColumn, LithologyStripColumn, type StratUnit, type LithologyInterval } from '@/Components/Foundry/Charts';
import { WorkspaceMap, type MapProjectInfo, type MapProjectSummary, type MapCollar, type BasemapId } from '@/Components/Foundry/WorkspaceMap';
import { CompareHolesModal } from '@/Components/Foundry/CompareHolesModal';
import { SectionView } from '@/Components/Foundry/SectionView';
import { Borehole3DView } from '@/Components/Foundry/Borehole3DView';
import { useFullscreenToggle } from '@/Hooks/useFullscreenToggle';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';

// Heavy Plotly-backed 3D sub-views — lazy-loaded so the workspace shell
// stays small and only pays the Plotly cost when the user enters 3D mode
// and selects the corresponding sub-view.
const MultiHole3DTrace = lazy(() => import('@/Components/Analytics/MultiHole3DTrace'));
const Stereosphere = lazy(() => import('@/Components/HoleAnalysis/Stereosphere'));
const OrientationSpiral = lazy(() => import('@/Components/HoleAnalysis/OrientationSpiral'));
const AggregateStereonet = lazy(() => import('@/Components/Analytics/AggregateStereonet'));
const AssayComposites3DView = lazy(() => import('@/Components/Foundry/AssayComposites3DView'));
const SignificantIntersections3DView = lazy(() => import('@/Components/Foundry/SignificantIntersections3DView'));
const StructureDiscs3DView = lazy(() => import('@/Components/Foundry/StructureDiscs3DView'));
const CommoditySamples3DView = lazy(() => import('@/Components/Foundry/CommoditySamples3DView'));

interface Collar {
    collar_id: string;
    hole_id: string;
    hole_id_canonical: string;
    easting: number | null;
    northing: number | null;
    total_depth: number | null;
    lat: number | null;
    lng: number | null;
    ore_bands: number;
    ore_thickness_m: number;
    azimuth?: number | null;
    dip?: number | null;
    elevation?: number | null;
    hole_type?: string | null;
    status?: string | null;
}

interface Survey3D {
    collar_id: string;
    depth: number;
    azimuth: number | null;
    dip: number | null;
}

interface Structure3D {
    collar_id: string;
    depth: number;
    structure_type: string;
    true_dip: number | null;
    dip_direction: number | null;
    description?: string | null;
}

interface AssayComposite3D {
    collar_id: string;
    element: string;
    from_depth: number;
    to_depth: number;
    weighted_avg: number;
    unit: string;
    cutoff_grade: number | null;
    sample_count: number | null;
}

interface AssayElement3D {
    element: string;
    count: number;
}

interface SignificantIntersection3D {
    collar_id: string;
    element: string;
    cutoff_grade: number;
    from_depth: number;
    to_depth: number;
    true_width_m: number | null;
    weighted_avg: number;
    unit: string;
    peak_value: number | null;
    peak_depth: number | null;
    zone_name: string | null;
}

interface StructureVisual3D {
    collar_id: string;
    strike_deg: number;
    dip_deg: number;
    measurement_kind: string;
    depth_m: number | null;
    pole_trend_deg: number;
    pole_plunge_deg: number;
    display_color: string | null;
    display_symbol: string | null;
    confidence: string | null;
}

interface CommoditySample3D {
    collar_id: string;
    from_depth: number;
    to_depth: number;
    sample_type: string;
    grades: Record<string, number>;
}

interface CommodityKey3D {
    key: string;
    count: number;
}

interface PgeoLayer {
    id: string;
    label: string;
    tier: number;
    on: boolean;
    locked?: boolean;
}

interface ProjectLayer {
    id: string;
    label: string;
    count: number;
    on: boolean;
}

interface HoleIntervalBand {
    from: number;
    to: number;
    code: string;
    color: string;
}

interface HoleIntervals {
    hole_id: string;
    total_depth: number | null;
    easting: number | null;
    northing: number | null;
    lat: number | null;
    lng: number | null;
    bands: HoleIntervalBand[];
}

interface CurveSummaryRow {
    curve_name: string;
    curves: number;
    avg_samples: number;
}

interface LogTrack {
    label: string;
    color: string;
    points: Array<{ depth: number; value: number }>;
    min: number;
    max: number;
}

interface WorkspaceProps {
    project: {
        project_id: string;
        project_name: string;
        slug: string;
        company: string | null;
        commodity: string | null;
        region: string | null;
        crs_epsg: number | null;
    };
    project_summary: MapProjectSummary;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    project_aoi: any | null;
    collars: Collar[];
    sections_count: number;
    intervals_count: number;
    structures_count: number;
    structures_visual_count: number;
    well_log_curves_count: number;
    curve_summary: CurveSummaryRow[];
    log_tracks: LogTrack[];
    log_hole_id: string | null;
    log_depth_max: number;
    log_hole_options: string[];
    log_hole_total_depth: number | null;
    log_hole_easting: number | null;
    log_hole_northing: number | null;
    log_lithology_intervals: LithologyInterval[];
    first_holes_intervals: HoleIntervals[];
    project_layers: ProjectLayer[];
    strat_units: StratUnit[];
    strat_source: 'project' | 'reference';
    pgeo_layers: PgeoLayer[];
    pgeo_note: string | null;
    pgeo_country: 'CA' | 'US' | 'OTHER';
    surveys_3d: Survey3D[];
    structures_3d: Structure3D[];
    assay_composites_3d: AssayComposite3D[];
    assay_elements_3d: AssayElement3D[];
    significant_intersections_3d: SignificantIntersection3D[];
    structures_visual_3d: StructureVisual3D[];
    commodity_samples_3d: CommoditySample3D[];
    commodity_keys_3d: CommodityKey3D[];
    empty: boolean;
}

type View3D =
    | 'lithology'
    | 'trajectories'
    | 'spiral'
    | 'stereosphere'
    | 'project_stereonet'
    | 'assay_grade'
    | 'significant_intersections'
    | 'structure_discs'
    | 'commodity_samples';

type Mode = 'map' | 'section' | '3d' | 'structure' | 'logs';
type Tool = 'pan' | 'draw' | 'measure' | 'select';

export default function FoundryWorkspace({ project, project_summary, project_aoi, collars, sections_count, intervals_count, structures_count, structures_visual_count, well_log_curves_count, curve_summary, log_tracks, log_hole_id, log_depth_max, log_hole_options, log_hole_total_depth, log_hole_easting, log_hole_northing, log_lithology_intervals, first_holes_intervals, project_layers, strat_units, strat_source, pgeo_layers, pgeo_note, pgeo_country, surveys_3d, structures_3d, assay_composites_3d, assay_elements_3d, significant_intersections_3d, structures_visual_3d, commodity_samples_3d, commodity_keys_3d, empty }: WorkspaceProps) {
    // Phase 5 real-time push — sync_silver_to_kg / mv_refresh_silver /
    // ingest jobs all touch the 3D mode's 9 sub-views. Full reload is
    // acceptable given the large prop surface (per Phase 5 decision).
    useWorkspaceDataUpdated(project.project_id, (event) => {
        if (event.affected_types.includes('collars') || event.affected_types.includes('reports')) {
            router.reload();
        }
    });

    const [mode, setMode] = useState<Mode>('map');
    const [view3d, setView3d] = useState<View3D>('lithology');
    const [tool, setTool] = useState<Tool>('pan');
    const [layersOn, setLayersOn] = useState<Record<string, boolean>>({});
    const [projectLayersOn, setProjectLayersOn] = useState<Record<string, boolean>>(
        () => Object.fromEntries(project_layers.map((l) => [l.id, l.on])),
    );
    const [copilotOpen, setCopilotOpen] = useState(true);
    const [copilotPrompt, setCopilotPrompt] = useState('');
    const [compareSet, setCompareSet] = useState<string[]>([]);
    const [compareOpen, setCompareOpen] = useState(false);
    const [basemap, setBasemap] = useState<BasemapId>('dark_matter');
    const [terrainOn, setTerrainOn] = useState(false);
    // activeHole lifted up from WorkspaceMap so the compare-close handlers
    // can restore the popup to the original hole after dismissing the modal.
    const [activeHole, setActiveHole] = useState<MapCollar | null>(null);
    // Fullscreen-within-app: hides PageHeader + both asides; the mode
    // toolbar stays visible so the user can switch between map/section/
    // 3d/structure/logs without exiting fullscreen. Esc exits.
    const { isFullscreen: isCanvasFullscreen, toggle: toggleCanvasFullscreen } = useFullscreenToggle();

    // Modes the user has visited at least once. We mount each mode's
    // content lazily on first visit, then keep it mounted (toggling
    // display: none for non-active modes). Avoids the heavy
    // teardown+rebuild every switch — MapLibre instance, Plotly 3D
    // scene, and SectionView fetches all persist between switches.
    const [visitedModes, setVisitedModes] = useState<Set<Mode>>(() => new Set<Mode>(['map']));
    useEffect(() => {
        setVisitedModes((prev) => {
            if (prev.has(mode)) return prev;
            const next = new Set(prev);
            next.add(mode);
            return next;
        });
    }, [mode]);

    // Viewport-derived chart height for LOGS mode. The page chrome is:
    //   org bar 44 + project sub-bar 36 + page header ~88 + toolbar ~52 +
    //   canvas padding 48 + card header ~48 + card body padding 32 +
    //   hole picker + curve-summary text ~70 ≈ 418 px.
    // We give the charts the rest so they breathe on tall windows and
    // stay compact on short ones (clamped to a sane minimum).
    const [chartH, setChartH] = useState<number>(() =>
        typeof window === 'undefined' ? 520 : Math.max(380, window.innerHeight - 420),
    );
    useEffect(() => {
        function onResize() {
            setChartH(Math.max(380, window.innerHeight - 420));
        }
        window.addEventListener('resize', onResize);
        return () => window.removeEventListener('resize', onResize);
    }, []);

    function toggleCompare(holeId: string) {
        setCompareSet((prev) => {
            if (prev.includes(holeId)) {
                return prev.filter((h) => h !== holeId);
            }
            if (prev.length >= 2) return prev;
            const next = [...prev, holeId];
            // Auto-open when we have the pair queued.
            if (next.length === 2) {
                setCompareOpen(true);
            }
            return next;
        });
    }

    function findCollar(holeId: string | null): MapCollar | null {
        if (!holeId) return null;
        const c = collars.find((c) => c.hole_id_canonical === holeId || c.hole_id === holeId);
        return c ?? null;
    }

    // Mount-on-first-visit + display:none for non-active modes. Returns
    // null until the user has selected this mode at least once, then keeps
    // the panel mounted with display: none when another mode is active so
    // expensive children (MapLibre, Plotly) don't tear down + rebuild on
    // each switch.
    function renderModePanel(target: Mode, content: React.ReactNode) {
        if (!visitedModes.has(target)) return null;
        const visible = mode === target;

        return (
            <div
                key={target}
                style={{
                    display: visible ? 'flex' : 'none',
                    flex: visible ? 1 : '0 0 auto',
                    flexDirection: 'column',
                    minHeight: 0,
                    overflow: 'hidden',
                }}
            >
                {content}
            </div>
        );
    }

    function closeCompareKeepOriginal() {
        // Close modal + clear queue + restore the original (first-queued)
        // hole's popup so the user can see what they were inspecting before
        // the comparison. If for some reason the queue is empty, just close.
        const originalHoleId = compareSet[0] ?? null;
        setCompareOpen(false);
        setCompareSet([]);
        const original = findCollar(originalHoleId);
        if (original) {
            setActiveHole(original);
        }
    }

    return (
        <AppLayout>
            <Head title={`Workspace · ${project.project_name}`} />

            <div className="flex-1 flex flex-col overflow-hidden" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                {!isCanvasFullscreen && (
                    <PageHeader
                        eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · WORKSPACE`}
                        title="Project canvas"
                        sub={`${collars.length} collars · ${well_log_curves_count} log curves · ${sections_count} section panels · ${structures_count} structures`}
                    />
                )}

                {/* Toolbar — mode + tool segments, side-by-side per the V2 layout.
                    Hidden in fullscreen; mode switching from there requires
                    Esc (or the floating Exit button) first. */}
                {!isCanvasFullscreen && (
                    <div className="flex items-center gap-3 px-8 py-2 border-b shrink-0" style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)' }}>
                        <span className="text-[10px] font-mono uppercase tracking-widest" style={{ color: 'var(--fg-3)' }}>Mode</span>
                        <Segmented<Mode>
                            value={mode}
                            onChange={setMode}
                            options={[
                                { value: 'map', label: 'Map' },
                                { value: 'section', label: 'Section' },
                                { value: '3d', label: '3D' },
                                { value: 'structure', label: 'Structure' },
                                { value: 'logs', label: 'Logs' },
                            ]}
                        />
                        <div className="flex-1" />
                        <button
                            type="button"
                            onClick={toggleCanvasFullscreen}
                            className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border"
                            style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)', background: 'var(--bg-2)' }}
                            title="Fullscreen canvas (Esc to exit)"
                        >
                            Fullscreen ⤢
                        </button>
                        <Link
                            href={`/projects/${project.slug}/saved-views`}
                            className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border"
                            style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                        >
                            Views
                        </Link>
                    </div>
                )}

                <div
                    className={
                        isCanvasFullscreen
                            ? 'fixed inset-0 z-[100] grid grid-cols-1 overflow-hidden'
                            : 'flex-1 grid grid-cols-[240px_1fr_320px] overflow-hidden'
                    }
                    style={isCanvasFullscreen ? { background: 'var(--bg-0)' } : undefined}
                >
                    {/* Layers panel */}
                    <aside
                        className={`border-r overflow-y-auto${isCanvasFullscreen ? ' hidden' : ''}`}
                        style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}
                    >
                        <div className="px-3 py-3 border-b text-[10px] font-mono uppercase tracking-[0.12em]" style={{ borderColor: 'var(--line-1)', color: 'var(--fg-3)' }}>
                            Layers
                        </div>
                        <div className="px-3 py-2">
                            <div className="text-[10px] font-mono uppercase tracking-wider mb-1.5" style={{ color: 'var(--fg-3)' }}>Project</div>
                            {project_layers.map((layer) => {
                                const has = layer.count > 0;
                                const checked = projectLayersOn[layer.id] ?? false;
                                return (
                                    <label key={layer.id} className={`flex items-center gap-2 py-1 text-xs ${has ? 'cursor-pointer' : 'cursor-default'}`}>
                                        <input
                                            type="checkbox"
                                            checked={checked}
                                            disabled={!has}
                                            onChange={(e) => setProjectLayersOn({ ...projectLayersOn, [layer.id]: e.target.checked })}
                                        />
                                        <span style={{ color: has ? 'var(--fg-1)' : 'var(--fg-3)' }}>{layer.label}</span>
                                        <span className="ml-auto text-[10px] font-mono" style={{ color: has ? 'var(--fg-2)' : 'var(--fg-3)' }}>
                                            {layer.count.toLocaleString()}
                                        </span>
                                    </label>
                                );
                            })}
                        </div>
                        {pgeo_layers.length === 0 ? (
                            <div className="px-3 py-3 border-t" style={{ borderColor: 'var(--line-1)' }}>
                                <div className="text-[10px] font-mono uppercase tracking-wider mb-1.5" style={{ color: 'var(--fg-3)' }}>
                                    Public geoscience · {pgeo_country}
                                </div>
                                <div className="text-[11px] leading-snug" style={{ color: 'var(--fg-3)' }}>
                                    {pgeo_note ?? 'No public geoscience layers available for this jurisdiction.'}
                                </div>
                            </div>
                        ) : (
                            <>
                                <div className="px-3 py-2 border-t" style={{ borderColor: 'var(--line-1)' }}>
                                    <div className="text-[10px] font-mono uppercase tracking-wider mb-1.5" style={{ color: 'var(--fg-3)' }}>Tier 2 · permissive</div>
                                    {pgeo_layers.filter((l) => l.tier === 2).map((l) => (
                                        <label key={l.id} className="flex items-center gap-2 py-1 text-xs cursor-pointer">
                                            <input type="checkbox" checked={layersOn[l.id] ?? l.on} onChange={(e) => setLayersOn({ ...layersOn, [l.id]: e.target.checked })} />
                                            <span style={{ color: 'var(--fg-1)' }}>{l.label}</span>
                                        </label>
                                    ))}
                                </div>
                                <div className="px-3 py-2 border-t" style={{ borderColor: 'var(--line-1)' }}>
                                    <div className="text-[10px] font-mono uppercase tracking-wider mb-1.5" style={{ color: 'var(--fg-3)' }}>Tier 3 · gated</div>
                                    {pgeo_layers.filter((l) => l.tier === 3).map((l) => (
                                        <div key={l.id} className="flex items-center gap-2 py-1 text-xs" style={{ color: 'var(--fg-3)' }}>
                                            <span>🔒</span>
                                            <span className="line-through">{l.label}</span>
                                        </div>
                                    ))}
                                    <Link
                                        href="/public-geoscience/tier3-unlock"
                                        className="text-[10px] font-mono uppercase tracking-wider mt-2 inline-block px-2 py-1 rounded border"
                                        style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                                    >
                                        Request Tier 3 access
                                    </Link>
                                </div>
                            </>
                        )}
                    </aside>

                    {/* Mode canvas — flex-col so map/charts can fill viewport.
                        Internal scroll lives on the chart card content,
                        not on the section, so the page never grows past 100vh. */}
                    <section className={`flex flex-col overflow-hidden min-h-0${isCanvasFullscreen ? ' p-0' : ' p-6'}`}>
                        {empty ? (
                            <EmptyState
                                title="No drill data in this project."
                                detail="Ingest LAS / SEG-Y / AGS / KMZ via Data → Connect Source to populate the workspace canvases."
                                action={<Link href={`/projects/${project.slug}/imports/quality`} className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border" style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}>Open import quality →</Link>}
                            />
                        ) : (
                            <>
                                {renderModePanel('map', (
                                    <Card
                                        eyebrow={`MAP · MAPLIBRE · ${collars.length} COLLARS`}
                                        title="Project collars on basemap"
                                        className="flex-1 flex flex-col min-h-0"
                                        contentClassName="flex-1 flex flex-col min-h-0"
                                    >
                                        <div className="text-[10px] font-mono mb-2 shrink-0" style={{ color: 'var(--fg-3)' }}>
                                            Click any collar for detail + jump to LOGS. Hover for tooltip.
                                            Layer toggles (left rail): "Collars" hides dots · "Ore-bearing holes only" filters to ore-bearing holes · "Ore heatmap" turns on the thickness heatmap.
                                        </div>
                                        <div className="flex-1 min-h-0">
                                        <WorkspaceMap
                                            collars={collars}
                                            projectSlug={project.slug}
                                            projectInfo={{
                                                project_name: project.project_name,
                                                company: project.company,
                                                commodity: project.commodity,
                                                region: project.region,
                                                crs_epsg: project.crs_epsg,
                                            }}
                                            projectSummary={project_summary}
                                            projectAoi={project_aoi}
                                            visibleLayers={projectLayersOn}
                                            activeHole={activeHole}
                                            setActiveHole={setActiveHole}
                                            compareSet={compareSet}
                                            onToggleCompare={toggleCompare}
                                            onOpenCompare={() => setCompareOpen(true)}
                                            onClearCompare={closeCompareKeepOriginal}
                                            basemap={basemap}
                                            onBasemapChange={setBasemap}
                                            terrainOn={terrainOn}
                                            onTerrainChange={setTerrainOn}
                                            activeTool={tool}
                                            onToolChange={setTool}
                                            onJumpToLogs={(holeId) => {
                                                setMode('logs');
                                                router.get(
                                                    `/projects/${project.slug}/workspace`,
                                                    { log_hole: holeId },
                                                    {
                                                        preserveScroll: true,
                                                        preserveState: true,
                                                        only: [
                                                            'log_tracks',
                                                            'log_hole_id',
                                                            'log_depth_max',
                                                            'log_hole_total_depth',
                                                            'log_hole_easting',
                                                            'log_hole_northing',
                                                            'log_lithology_intervals',
                                                        ],
                                                    },
                                                );
                                            }}
                                        />
                                        </div>
                                    </Card>
                                ))}
                                {renderModePanel('section', (
                                    <Card
                                        eyebrow={`SECTION · AD-HOC · ${log_hole_options.length} HOLES AVAILABLE`}
                                        title="2-hole cross section"
                                        className="flex-1 flex flex-col min-h-0"
                                        contentClassName="flex-1 flex flex-col min-h-0 overflow-hidden"
                                    >
                                        {log_hole_options.length >= 2 ? (
                                            <SectionView
                                                projectSlug={project.slug}
                                                holeOptions={log_hole_options}
                                                defaultLeft={log_hole_options[0]}
                                                defaultRight={log_hole_options[1]}
                                                chartH={chartH}
                                            />
                                        ) : (
                                            <EmptyState
                                                title="Need at least 2 collars to draw a section."
                                                detail="This project has fewer than 2 collars with GAMMA curves. Ingest more LAS files via Data → Connect Source."
                                            />
                                        )}
                                    </Card>
                                ))}
                                {renderModePanel('3d', (() => {
                                    // Resolve the "active hole" for the per-hole 3D sub-views
                                    // (Spiral). Prefer the LOGS panel's current hole if set,
                                    // otherwise fall back to the first collar with usable
                                    // azimuth/dip on the project.
                                    const spiralCollar = (() => {
                                        const target = log_hole_id;
                                        const match = collars.find((c) =>
                                            target ? (c.hole_id_canonical === target || c.hole_id === target) : false,
                                        );
                                        return match ?? collars[0] ?? null;
                                    })();
                                    const spiralSurveys = spiralCollar
                                        ? surveys_3d.filter((s) => s.collar_id === spiralCollar.collar_id)
                                        : [];
                                    return (
                                    <Card
                                        eyebrow={(() => {
                                            if (view3d === 'lithology') {
                                                return `3D · LITHOLOGY · ${intervals_count > 0 ? `${first_holes_intervals.length} HOLES · ${intervals_count} INTERVALS` : 'NO DATA'}`;
                                            }
                                            if (view3d === 'trajectories') {
                                                return `3D · TRAJECTORIES · ${collars.length} COLLARS · ${surveys_3d.length} SURVEY STATIONS`;
                                            }
                                            if (view3d === 'stereosphere') {
                                                return `3D · STEREOSPHERE · ${structures_3d.length} MEASUREMENTS`;
                                            }
                                            if (view3d === 'spiral') {
                                                const hid = spiralCollar ? (spiralCollar.hole_id_canonical || spiralCollar.hole_id) : '—';
                                                return `3D · ORIENTATION SPIRAL · HOLE ${hid} · ${spiralSurveys.length} STATIONS`;
                                            }
                                            if (view3d === 'project_stereonet') {
                                                return `3D · PROJECT STEREONET · ${structures_3d.length} MEASUREMENTS`;
                                            }
                                            if (view3d === 'assay_grade') {
                                                return `3D · ASSAY GRADE · ${assay_composites_3d.length} COMPOSITES · ${assay_elements_3d.length} ELEMENTS`;
                                            }
                                            if (view3d === 'significant_intersections') {
                                                return `3D · SIGNIFICANT INTERSECTIONS · ${significant_intersections_3d.length} HITS`;
                                            }
                                            if (view3d === 'structure_discs') {
                                                return `3D · STRUCTURE DISCS · ${structures_visual_3d.length} MEASUREMENTS`;
                                            }
                                            return `3D · COMMODITY SAMPLES · ${commodity_samples_3d.length} SAMPLES · ${commodity_keys_3d.length} COMMODITIES`;
                                        })()}
                                        title={(() => {
                                            if (view3d === 'lithology') return 'Borehole 3D viewer';
                                            if (view3d === 'trajectories') return '3D drill trajectories';
                                            if (view3d === 'stereosphere') return '3D stereosphere · lower hemisphere';
                                            if (view3d === 'spiral') return 'Per-hole 3D orientation spiral';
                                            if (view3d === 'project_stereonet') return 'Project-wide aggregate stereonet (2D + 3D)';
                                            if (view3d === 'assay_grade') return 'Assay composites · grade-coloured sticks';
                                            if (view3d === 'significant_intersections') return 'Significant cutoff-grade intersections';
                                            if (view3d === 'structure_discs') return 'Structure measurements · oriented discs in space';
                                            return 'Commodity grade samples (silver.samples)';
                                        })()}
                                        actions={(
                                            <Segmented<View3D>
                                                value={view3d}
                                                onChange={setView3d}
                                                options={[
                                                    { value: 'lithology', label: 'Lithology' },
                                                    { value: 'trajectories', label: 'Trajectories' },
                                                    { value: 'spiral', label: 'Spiral' },
                                                    { value: 'stereosphere', label: 'Stereosphere' },
                                                    { value: 'project_stereonet', label: 'Project Stereonet' },
                                                    { value: 'assay_grade', label: 'Assay Grade' },
                                                    { value: 'significant_intersections', label: 'Intersections' },
                                                    { value: 'structure_discs', label: 'Structure Discs' },
                                                    { value: 'commodity_samples', label: 'Commodity Samples' },
                                                ]}
                                            />
                                        )}
                                        className="flex-1 flex flex-col min-h-0"
                                        contentClassName="flex-1 flex flex-col min-h-0"
                                    >
                                        {view3d === 'lithology' && (
                                            intervals_count > 0 ? (
                                                <>
                                                    <div className="text-[11px] font-mono mb-3 shrink-0" style={{ color: 'var(--fg-3)' }}>
                                                        Each hole rendered as a vertical line coloured by derived lithology bands. Yellow segments = U-host (ore).
                                                        Drag to rotate, scroll to zoom, shift-drag to pan. Hover a band for hole ID / depth interval / lithology code.
                                                    </div>
                                                    <div className="flex-1 min-h-0">
                                                        <Borehole3DView holes={first_holes_intervals} height={chartH} />
                                                    </div>
                                                </>
                                            ) : (
                                                <EmptyState
                                                    title="0 rows in gold.drillhole_intervals_visual for this project."
                                                    detail="3D intervals are built by derive_intervals from the well-log curves. If curves exist but intervals don't, the derivation pipeline hasn't run for this project yet."
                                                />
                                            )
                                        )}
                                        {view3d === 'trajectories' && (
                                            collars.length > 0 ? (
                                                <>
                                                    <div className="text-[11px] font-mono mb-3 shrink-0" style={{ color: 'var(--fg-3)' }}>
                                                        Every drill hole projected from its collar in shared UTM space using azimuth + dip surveys.
                                                        Colour-coded by hole status — green = completed, amber = active, red = abandoned. Use it to spot
                                                        drilling-pattern gaps, overlapping targets, and overall campaign geometry.
                                                    </div>
                                                    <div className="flex-1 min-h-0">
                                                        <Suspense fallback={<EmptyState title="Loading 3D trajectories…" detail="" />}>
                                                            <MultiHole3DTrace
                                                                collars={collars.map((c) => ({
                                                                    collar_id: c.collar_id,
                                                                    hole_id: c.hole_id_canonical || c.hole_id,
                                                                    azimuth: c.azimuth ?? null,
                                                                    dip: c.dip ?? null,
                                                                    elevation: c.elevation ?? null,
                                                                    easting: c.easting,
                                                                    northing: c.northing,
                                                                    hole_type: c.hole_type ?? null,
                                                                    status: c.status ?? null,
                                                                }))}
                                                                surveys={surveys_3d}
                                                                colorBy="status"
                                                            />
                                                        </Suspense>
                                                    </div>
                                                </>
                                            ) : (
                                                <EmptyState
                                                    title="No collars to plot."
                                                    detail="Trajectories needs collars with easting/northing and at least one azimuth+dip survey station per hole."
                                                />
                                            )
                                        )}
                                        {view3d === 'stereosphere' && (
                                            structures_3d.length > 0 ? (
                                                <>
                                                    <div className="text-[11px] font-mono mb-3 shrink-0" style={{ color: 'var(--fg-3)' }}>
                                                        Planar measurements rendered as great-circle arcs on the lower hemisphere; lineations as point cloud.
                                                        Colour-coded by structure type. Drag to rotate, scroll to zoom — read structural geometry directly
                                                        rather than through a 2-D equal-area projection.
                                                    </div>
                                                    <div className="flex-1 min-h-0">
                                                        <Suspense fallback={<EmptyState title="Loading 3D stereosphere…" detail="" />}>
                                                            <Stereosphere
                                                                structures={structures_3d}
                                                                holeId={`project-${project.slug}`}
                                                            />
                                                        </Suspense>
                                                    </div>
                                                </>
                                            ) : (
                                                <EmptyState
                                                    title="No structural measurements ingested yet (system-wide)."
                                                    detail="The 3D stereosphere needs discrete planar features (bedding, foliation, joints, faults, veins) with true_dip + dip_direction. silver.structure has 0 rows across every project — the LAS/binary-log corpora carry per-depth downhole survey curves (used by Trajectories + Spiral) but not measured geological structures. Add via Data → Connect Source (CSV with strike/dip per depth) or QField, or wait for downstream extraction from descriptions."
                                                />
                                            )
                                        )}
                                        {view3d === 'spiral' && (
                                            spiralCollar && (spiralSurveys.length > 0 || (spiralCollar.azimuth != null && spiralCollar.dip != null)) ? (
                                                <>
                                                    <div className="text-[11px] font-mono mb-3 shrink-0" style={{ color: 'var(--fg-3)' }}>
                                                        Active hole's deviation surveys integrated into a 3-D minimum-curvature spiral.
                                                        Hole picked from the LOGS panel (or first collar by default). Useful for spotting
                                                        survey drift, dogleg severity, and how far the bit walked from its planned path.
                                                    </div>
                                                    <div className="flex-1 min-h-0">
                                                        <Suspense fallback={<EmptyState title="Loading orientation spiral…" detail="" />}>
                                                            <OrientationSpiral
                                                                surveys={spiralSurveys}
                                                                collarAzimuth={spiralCollar.azimuth ?? null}
                                                                collarDip={spiralCollar.dip ?? null}
                                                                collarElevation={spiralCollar.elevation ?? null}
                                                                totalDepth={spiralCollar.total_depth ?? null}
                                                                view="3d"
                                                            />
                                                        </Suspense>
                                                    </div>
                                                </>
                                            ) : (
                                                <EmptyState
                                                    title="Not enough survey data for an orientation spiral."
                                                    detail="Need at least one collar with azimuth + dip, plus survey stations (silver.surveys). The Wyoming Cameco corpus has AZIMUTH + SANG curves on well_log_curves but no parsed survey rows yet — derivation is the next pipeline step."
                                                />
                                            )
                                        )}
                                        {view3d === 'project_stereonet' && (
                                            structures_3d.length > 0 ? (
                                                <>
                                                    <div className="text-[11px] font-mono mb-3 shrink-0" style={{ color: 'var(--fg-3)' }}>
                                                        Project-wide aggregate of every structural measurement across every hole. Toggle 2D/3D
                                                        with the dimension switch on the left. Filter by structure type to isolate bedding,
                                                        foliation, joints, faults, shears, veins, or lineations.
                                                    </div>
                                                    <div className="flex-1 min-h-0 overflow-auto">
                                                        <Suspense fallback={<EmptyState title="Loading project stereonet…" detail="" />}>
                                                            <AggregateStereonet structures={structures_3d} />
                                                        </Suspense>
                                                    </div>
                                                </>
                                            ) : (
                                                <EmptyState
                                                    title="No structural measurements ingested yet (system-wide)."
                                                    detail="silver.structure has 0 rows across every project. The aggregate stereonet aggregates planar features (bedding/foliation/joint/fault) across every hole, but none have been logged. Add via Data → Connect Source (CSV/QGIS) or QField; the binary .log corpus carries deviation curves (used by Trajectories + Spiral) but not measured structures."
                                                />
                                            )
                                        )}
                                        {view3d === 'assay_grade' && (
                                            assay_elements_3d.length > 0 ? (
                                                <>
                                                    <div className="text-[11px] font-mono mb-3 shrink-0" style={{ color: 'var(--fg-3)' }}>
                                                        Composited assay grades from <span className="font-bold">gold.assay_composites</span>.
                                                        Each band on each hole is coloured by the composite's weighted-average grade for the
                                                        selected element. Compare with the Lithology view — Lithology shows derived rock type,
                                                        this shows real assayed grade.
                                                    </div>
                                                    <div className="flex-1 min-h-0">
                                                        <Suspense fallback={<EmptyState title="Loading assay composites…" detail="" />}>
                                                            <AssayComposites3DView
                                                                collars={collars}
                                                                composites={assay_composites_3d}
                                                                elements={assay_elements_3d}
                                                                height={chartH}
                                                            />
                                                        </Suspense>
                                                    </div>
                                                </>
                                            ) : (
                                                <EmptyState
                                                    title="0 rows in gold.assay_composites for this project."
                                                    detail="The composite pipeline (compute_assay_composites Dagster asset) hasn't run for this project yet. Composites are derived from silver.assays_v2 at common cutoffs per element."
                                                />
                                            )
                                        )}
                                        {view3d === 'significant_intersections' && (
                                            significant_intersections_3d.length > 0 ? (
                                                <>
                                                    <div className="text-[11px] font-mono mb-3 shrink-0" style={{ color: 'var(--fg-3)' }}>
                                                        Cutoff-grade hits from <span className="font-bold">gold.significant_intersections</span>.
                                                        Ghost-rendered hole sticks with each significant interval glowing in heat-palette colour
                                                        by weighted-average grade. White marker = peak grade depth. Use it to spot which holes
                                                        hit ore-grade mineralisation and where.
                                                    </div>
                                                    <div className="flex-1 min-h-0">
                                                        <Suspense fallback={<EmptyState title="Loading significant intersections…" detail="" />}>
                                                            <SignificantIntersections3DView
                                                                collars={collars}
                                                                intersections={significant_intersections_3d}
                                                                height={chartH}
                                                            />
                                                        </Suspense>
                                                    </div>
                                                </>
                                            ) : (
                                                <EmptyState
                                                    title="0 rows in gold.significant_intersections for this project."
                                                    detail="The promote_significant_intersections Dagster asset hasn't run. Once it does, every cutoff-grade hit per hole shows up here as a highlight ribbon."
                                                />
                                            )
                                        )}
                                        {view3d === 'commodity_samples' && (
                                            commodity_keys_3d.length > 0 ? (
                                                <>
                                                    <div className="text-[11px] font-mono mb-3 shrink-0" style={{ color: 'var(--fg-3)' }}>
                                                        Commodity grades per sample interval from <span className="font-bold">silver.samples</span>.
                                                        For Cameco this is where uranium grade (U3O8_pct_e) actually lives — gold.assay_composites
                                                        is REE/base-metals only. Pick a commodity to see grade variation along every hole.
                                                    </div>
                                                    <div className="flex-1 min-h-0">
                                                        <Suspense fallback={<EmptyState title="Loading commodity samples…" detail="" />}>
                                                            <CommoditySamples3DView
                                                                collars={collars}
                                                                samples={commodity_samples_3d}
                                                                commodityKeys={commodity_keys_3d}
                                                                height={chartH}
                                                            />
                                                        </Suspense>
                                                    </div>
                                                </>
                                            ) : (
                                                <EmptyState
                                                    title="0 commodity samples in silver.samples for this project."
                                                    detail="silver.samples stores per-interval commodity grades (e.g. U3O8_pct_e, Au_gpt). Ingest CSV/QGIS assay samples via Data → Connect Source, or wait for downstream composite derivation."
                                                />
                                            )
                                        )}
                                        {view3d === 'structure_discs' && (
                                            structures_visual_3d.length > 0 ? (
                                                <>
                                                    <div className="text-[11px] font-mono mb-3 shrink-0" style={{ color: 'var(--fg-3)' }}>
                                                        Each measurement from <span className="font-bold">gold.structure_measurements_visual</span>
                                                        rendered as an oriented disc in the plane perpendicular to its pole, positioned at the
                                                        measurement depth on its collar. Different from Stereosphere — that abstracts onto a unit
                                                        sphere; this anchors in real space so spatial clustering is visible.
                                                    </div>
                                                    <div className="flex-1 min-h-0">
                                                        <Suspense fallback={<EmptyState title="Loading structure discs…" detail="" />}>
                                                            <StructureDiscs3DView
                                                                collars={collars}
                                                                structures={structures_visual_3d}
                                                                height={chartH}
                                                            />
                                                        </Suspense>
                                                    </div>
                                                </>
                                            ) : (
                                                <EmptyState
                                                    title="0 rows in gold.structure_measurements_visual for this project."
                                                    detail="Needs the structure-visual enrichment pipeline to run. Until then, the silver-tier Stereosphere + Project Stereonet sub-views still work from silver.structures."
                                                />
                                            )
                                        )}
                                    </Card>
                                    );
                                })())}
                                {renderModePanel('structure', (
                                    structures_count > 0 || structures_visual_count > 0 ? (
                                        <div className="grid grid-cols-2 gap-4">
                                            <Card eyebrow={`STEREONET · ${structures_count} measurements`} title="Schmidt equal-area">
                                                {/* TODO: backend doesn't return structure measurement arrays yet —
                                                    when silver.structures or gold.structure_measurements_visual gets
                                                    rows, controller needs to emit `structures_measurements` prop and
                                                    we pass it here instead of []. */}
                                                <StereonetMini measurements={[]} size={260} />
                                                <div className="text-[10px] font-mono mt-2" style={{ color: 'var(--fg-3)' }}>
                                                    Backend payload TODO: structure measurement arrays not yet emitted by WorkspaceController.
                                                </div>
                                            </Card>
                                            <Card eyebrow={`ROSE DIAGRAM · ${structures_count} strikes`} title="Strike frequency">
                                                <RoseMini strikes={[]} size={260} />
                                                <div className="text-[10px] font-mono mt-2" style={{ color: 'var(--fg-3)' }}>
                                                    Same — strike array not yet wired through.
                                                </div>
                                            </Card>
                                        </div>
                                    ) : (
                                        <Card eyebrow="STRUCTURE" title="No structure measurements yet">
                                            <EmptyState
                                                title="0 rows in silver.structures + gold.structure_measurements_visual."
                                                detail="The Cameco binary .log corpus contains AZIMUTH + SANG (survey angle) curves on every hole, but explicit structure measurements (joints, foliations, faults) require manual logging or downstream extraction. Coming-soon: derive proxy orientation from the deviation surveys."
                                            />
                                        </Card>
                                    )
                                ))}
                                {renderModePanel('logs', (
                                    <Card
                                        eyebrow={log_hole_id ? `LOGS · HOLE ${log_hole_id}` : 'LOGS'}
                                        title={log_tracks.length > 0 ? `${log_tracks.length} curves rendered · ${well_log_curves_count} total in project` : 'No curve data'}
                                        className="flex-1 flex flex-col min-h-0"
                                        contentClassName="flex-1 flex flex-col min-h-0"
                                    >
                                        {log_hole_options.length > 0 && (
                                            <LogsHolePicker
                                                projectSlug={project.slug}
                                                activeHoleId={log_hole_id}
                                                holes={log_hole_options}
                                            />
                                        )}
                                        {log_tracks.length > 0 ? (
                                            <>
                                                <div className="text-[11px] font-mono mb-3 shrink-0" style={{ color: 'var(--fg-3)' }}>
                                                    Curves available across project: {curve_summary.map((c) => `${c.curve_name} (${c.curves})`).join(' · ')}
                                                </div>
                                                <div className="flex gap-6 overflow-auto items-start flex-1 min-h-0 py-1 px-1">
                                                    <div className="shrink-0">
                                                        <DownholeMultiLog tracks={log_tracks} depthMax={log_depth_max} height={chartH} trackWidth={96} />
                                                    </div>
                                                    <div className="shrink-0">
                                                        <LithologyStripColumn
                                                            intervals={log_lithology_intervals}
                                                            holeId={log_hole_id}
                                                            depthMax={log_depth_max}
                                                            height={chartH}
                                                            width={380}
                                                        />
                                                    </div>
                                                    <div className="shrink-0 flex flex-col gap-3" style={{ width: 420 }}>
                                                        <div
                                                            className="text-[11px] font-mono px-4 py-3 rounded border"
                                                            style={{ borderColor: 'var(--line-1)', background: 'var(--bg-2)', color: 'var(--fg-2)' }}
                                                        >
                                                            <div className="uppercase tracking-wider mb-1" style={{ color: 'var(--fg-3)' }}>Hole context</div>
                                                            <div className="text-sm" style={{ color: 'var(--fg-0)' }}>
                                                                {log_hole_id ?? '—'}
                                                                {log_hole_total_depth !== null && (
                                                                    <span style={{ color: 'var(--fg-2)' }}> · TD {log_hole_total_depth.toFixed(1)} m</span>
                                                                )}
                                                            </div>
                                                            {(log_hole_easting !== null && log_hole_northing !== null) && (
                                                                <div className="mt-1.5" style={{ color: 'var(--fg-3)' }}>
                                                                    UTM 13N · E {Math.round(log_hole_easting).toLocaleString()} · N {Math.round(log_hole_northing).toLocaleString()}
                                                                </div>
                                                            )}
                                                        </div>
                                                        <ChronoColumn
                                                            units={strat_units}
                                                            height={Math.max(360, chartH - 100)}
                                                            width={420}
                                                            eyebrow={strat_source === 'project' ? 'Project chronostratigraphy' : `Regional reference · ${pgeo_country === 'US' ? 'Wyoming roll-front uranium' : 'Athabasca / Wollaston Domain'}`}
                                                            title={strat_source === 'project' ? 'Stratigraphic column' : (pgeo_country === 'US' ? 'Shirley / PRB / WRB roll-front host stack' : 'Athabasca Group · Wollaston Domain')}
                                                        />
                                                    </div>
                                                </div>
                                                {strat_source === 'reference' && (
                                                    <div className="text-[10px] font-mono mt-2 shrink-0" style={{ color: 'var(--fg-3)' }}>
                                                        Chrono column = regional reference (silver.geological_formations has 0 rows for this project).
                                                    </div>
                                                )}
                                            </>
                                        ) : (
                                            <EmptyState
                                                title="No GAMMA / GRADE / RES / SP curves found for this project."
                                                detail="LOGS mode reads silver.well_log_curves filtered to the four uranium-relevant tracks. Ingest LAS files via Data → Connect Source to populate."
                                            />
                                        )}
                                    </Card>
                                ))}
                            </>
                        )}
                    </section>

                    {/* Copilot dock */}
                    <aside
                        className={`border-l flex flex-col overflow-hidden${isCanvasFullscreen ? ' hidden' : ''}`}
                        style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}
                    >
                        <div className="px-3 py-3 border-b flex items-center" style={{ borderColor: 'var(--line-1)' }}>
                            <span className="text-[10px] font-mono uppercase tracking-[0.12em] flex-1" style={{ color: 'var(--fg-3)' }}>Copilot</span>
                            <button type="button" onClick={() => setCopilotOpen((v) => !v)} className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-2)' }}>
                                {copilotOpen ? '−' : '+'}
                            </button>
                        </div>
                        {copilotOpen && (
                            <>
                                <div className="flex-1 overflow-y-auto px-3 py-2 text-xs space-y-2" style={{ color: 'var(--fg-2)' }}>
                                    <div className="px-2 py-1.5 rounded" style={{ background: 'var(--bg-2)' }}>
                                        <Pill tone="accent" dot>READY</Pill>
                                        <div className="mt-1 text-xs">
                                            Ask about <span style={{ color: 'var(--fg-0)' }}>{project.project_name}</span> — geology, holes, ore zones, or analogues.
                                        </div>
                                    </div>
                                    <div className="text-[10px] font-mono uppercase tracking-wider pt-2" style={{ color: 'var(--fg-3)' }}>Quick prompts</div>
                                    {[
                                        'Summarise the ore zones in this project',
                                        'Which holes have U₃O₈ > 0.05% intervals?',
                                        'Compare this project to Smith Ranch-Highland',
                                    ].map((q) => (
                                        <Link
                                            key={q}
                                            href={`/projects/${project.slug}/chat?prompt=${encodeURIComponent(q)}`}
                                            className="block text-left text-[11px] px-2 py-1.5 rounded border hover:opacity-80"
                                            style={{ borderColor: 'var(--line-1)', color: 'var(--fg-1)', background: 'var(--bg-2)' }}
                                        >
                                            {q}
                                        </Link>
                                    ))}
                                </div>
                                <form
                                    onSubmit={(e) => {
                                        e.preventDefault();
                                        if (!copilotPrompt.trim()) return;
                                        window.location.href = `/projects/${project.slug}/chat?prompt=${encodeURIComponent(copilotPrompt)}`;
                                    }}
                                    className="border-t px-3 py-2 flex flex-col gap-2"
                                    style={{ borderColor: 'var(--line-1)' }}
                                >
                                    <input
                                        value={copilotPrompt}
                                        onChange={(e) => setCopilotPrompt(e.target.value)}
                                        placeholder="Ask a question…"
                                        className="text-xs px-2 py-1.5 rounded border"
                                        style={{ borderColor: 'var(--line-2)', color: 'var(--fg-0)', background: 'var(--bg-2)' }}
                                    />
                                    <div className="flex gap-2">
                                        <button
                                            type="submit"
                                            disabled={!copilotPrompt.trim()}
                                            className="flex-1 text-[10px] font-mono uppercase tracking-wider px-2 py-1.5 rounded border disabled:opacity-40"
                                            style={{ color: 'var(--accent)', borderColor: 'var(--accent-dim)', background: 'var(--accent-bg)' }}
                                        >
                                            Ask →
                                        </button>
                                        <Link
                                            href={`/projects/${project.slug}/chat`}
                                            className="text-[10px] font-mono uppercase tracking-wider px-2 py-1.5 rounded border"
                                            style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                                        >
                                            Full chat
                                        </Link>
                                    </div>
                                </form>
                            </>
                        )}
                    </aside>
                </div>
            </div>
            {/* Floating Exit button when canvas is fullscreen — the
                Mode toolbar (where the enter-fullscreen button lives) is
                hidden in that state, so the user needs another way out
                besides Esc. Top-right keeps it clear of any in-map UI. */}
            {isCanvasFullscreen && (
                <button
                    type="button"
                    onClick={toggleCanvasFullscreen}
                    className="fixed top-3 right-3 z-[110] text-[10px] font-mono uppercase tracking-wider px-3 py-1.5 rounded border shadow-lg"
                    style={{ background: 'var(--bg-1)', borderColor: 'var(--line-2)', color: 'var(--fg-1)' }}
                    title="Exit fullscreen (Esc)"
                >
                    Exit fullscreen ⤡
                </button>
            )}
            {compareOpen && compareSet.length === 2 && (
                <CompareHolesModal
                    projectSlug={project.slug}
                    leftHole={compareSet[0]}
                    rightHole={compareSet[1]}
                    onClose={closeCompareKeepOriginal}
                />
            )}
        </AppLayout>
    );
}

function LogsHolePicker({ projectSlug, activeHoleId, holes }: { projectSlug: string; activeHoleId: string | null; holes: string[] }) {
    const idx = activeHoleId ? holes.indexOf(activeHoleId) : -1;
    const prev = idx > 0 ? holes[idx - 1] : null;
    const next = idx >= 0 && idx < holes.length - 1 ? holes[idx + 1] : null;

    function jumpTo(hole: string | null) {
        if (!hole) return;
        router.get(
            `/projects/${projectSlug}/workspace`,
            { log_hole: hole },
            {
                preserveScroll: true,
                preserveState: true,
                only: [
                    'log_tracks',
                    'log_hole_id',
                    'log_depth_max',
                    'log_hole_total_depth',
                    'log_hole_easting',
                    'log_hole_northing',
                    'log_lithology_intervals',
                ],
            },
        );
    }

    return (
        <div className="flex items-center gap-2 mb-3 flex-wrap" style={{ color: 'var(--fg-2)' }}>
            <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                Hole {idx >= 0 ? idx + 1 : 0} / {holes.length}
            </span>
            <button
                type="button"
                disabled={!prev}
                onClick={() => jumpTo(prev)}
                className="text-[11px] font-mono px-2 py-1 rounded border disabled:opacity-30"
                style={{ borderColor: 'var(--line-2)', color: 'var(--fg-2)', background: 'var(--bg-2)' }}
            >
                ← prev
            </button>
            <select
                value={activeHoleId ?? ''}
                onChange={(e) => jumpTo(e.target.value)}
                className="text-[11px] font-mono px-2 py-1 rounded border"
                style={{ borderColor: 'var(--line-2)', color: 'var(--fg-1)', background: 'var(--bg-2)' }}
            >
                {holes.map((h) => (
                    <option key={h} value={h}>{h}</option>
                ))}
            </select>
            <button
                type="button"
                disabled={!next}
                onClick={() => jumpTo(next)}
                className="text-[11px] font-mono px-2 py-1 rounded border disabled:opacity-30"
                style={{ borderColor: 'var(--line-2)', color: 'var(--fg-2)', background: 'var(--bg-2)' }}
            >
                next →
            </button>
        </div>
    );
}

function MiniHoleStrip({ hole, onClick }: { hole: HoleIntervals; onClick?: () => void }) {
    const totalDepth = Math.max(
        hole.total_depth ?? 0,
        ...hole.bands.map((b) => b.to),
        1,
    );
    const H = 220;
    const W = 32;
    const oreCount = hole.bands.filter((b) => b.code.endsWith('-ORE')).length;

    return (
        <button
            type="button"
            onClick={onClick}
            className="shrink-0 flex flex-col items-center cursor-pointer hover:opacity-90 transition-opacity"
            style={{ background: 'transparent', border: 0, padding: 0 }}
            title={`${hole.hole_id} · ${oreCount} U bands · click to open in LOGS`}
        >
            <div className="text-[9px] font-mono mb-0.5" style={{ color: 'var(--fg-3)' }}>{hole.hole_id}</div>
            <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} style={{ background: 'var(--bg-2)', border: '1px solid var(--line-1)', borderRadius: 2 }}>
                {hole.bands.map((b, i) => {
                    const y1 = (b.from / totalDepth) * H;
                    const y2 = (b.to / totalDepth) * H;
                    const h = Math.max(0.5, y2 - y1);
                    const isOre = b.code.endsWith('-ORE');
                    return (
                        <rect
                            key={i}
                            x={0}
                            y={y1}
                            width={W}
                            height={h}
                            fill={b.color}
                            stroke={isOre ? '#fff' : 'rgba(0,0,0,0.15)'}
                            strokeWidth={isOre ? '0.5' : '0.2'}
                        />
                    );
                })}
            </svg>
            <div className="text-[8px] font-mono mt-0.5" style={{ color: 'var(--fg-3)' }}>
                {hole.total_depth !== null ? `${hole.total_depth.toFixed(0)} m` : '—'}
            </div>
        </button>
    );
}
