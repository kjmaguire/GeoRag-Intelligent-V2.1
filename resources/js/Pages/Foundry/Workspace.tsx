import { Suspense, lazy, useEffect, useMemo, useRef, useState } from 'react';
import { Deferred, Head, Link, router } from '@inertiajs/react';
// 2026-08-17 — restored after the 2026-07-27 reader-core trim (see plan
// addendum). One change from the original: the toolbar's "Views" link to
// /projects/{slug}/saved-views was dropped — its controller
// (SavedMapViewController) was a pure stub whose every method threw
// LogicException even before deletion, and saved map views were never in
// this restoration's scope. Everything else below is unmodified.
import { PageHeader, Card, Pill, Segmented, EmptyState } from '@/Components/Foundry/primitives';
import {
    StereonetMini,
    RoseMini,
    DownholeMultiLog,
    ChronoColumn,
    LithologyStripColumn,
    geologyDepth,
    sharedDepthAxis,
    type StratUnit,
    type LithologyInterval,
    type StereonetPole,
} from '@/Components/Foundry/Charts';
import type { StripAlterationBand, StripMineralBand } from '@/lib/stripLog';
import {
    WorkspaceMap,
    type MapProjectSummary,
    type MapCollar,
    type BasemapId,
} from '@/Components/Foundry/WorkspaceMap';
import { CompareHolesModal, CompareHolesPanel } from '@/Components/Foundry/CompareHolesModal';
import { SectionView } from '@/Components/Foundry/SectionView';
import WorkspaceModeBar from '@/Components/Foundry/WorkspaceModeBar';
import { LogCurveToggles, type AvailableLogCurve } from '@/Components/Foundry/LogCurveToggles';
import { describeTerrainElevation, describeTruncation, type WorkspaceTruncation } from '@/lib/workspaceLimits';
import { useFullscreenToggle } from '@/Hooks/useFullscreenToggle';
import { structurePoles, structureStrikes } from '@/lib/structureProjection';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';
import { LOG_PROPS, VIZ3D_PROPS, copilotQuickPrompts, crsLabel, initialView3D, reloadPlan } from '@/lib/workspacePage';
import { hasNonCollarMapData, type LonLatBounds } from '@/lib/workspaceMapView';

// Heavy Plotly-backed 3D sub-views — lazy-loaded so the workspace shell
// stays small and only pays the Plotly cost when the user enters 3D mode
// and selects the corresponding sub-view.
// Borehole3DView too: it was the one static import here, and it pulled the
// 4.6 MB Plotly chunk into every Workspace visit, MAP mode included (FE-10).
const Borehole3DView = lazy(() => import('@/Components/Foundry/Borehole3DView'));
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
    /** 'terrain' = no elevation in the file; `elevation` is the terrain model's. */
    elevation_source?: 'file' | 'terrain' | null;
    /** The terrain model that gave `elevation` when `elevation_source` is 'terrain'. */
    elevation_dem_source?: string | null;
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
    collar_id?: string;
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
    curve?: string;
    group?: string;
    unit?: string | null;
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
        data_version?: number;
    };
    /** [west, south, east, north] of non-collar map data; set when no collar has a position. */
    project_extent?: LonLatBounds | null;
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
    log_available_curves: AvailableLogCurve[];
    log_selected_curves: string[];
    log_curves_max: number;
    log_hole_id: string | null;
    log_depth_max: number;
    log_hole_options: string[];
    log_hole_total_depth: number | null;
    log_hole_easting: number | null;
    log_hole_northing: number | null;
    log_lithology_intervals: LithologyInterval[];
    log_alteration_intervals: StripAlterationBand[];
    log_mineralization_intervals: StripMineralBand[];
    log_tracks_truncated?: { lithology?: boolean; alteration?: boolean; mineralization?: boolean };
    // ── deferred `viz3d` group (FE-11): undefined until it arrives ──
    first_holes_intervals?: HoleIntervals[];
    project_layers: ProjectLayer[];
    strat_units: StratUnit[];
    strat_source: 'project' | 'reference';
    project_country: 'CA' | 'US' | 'OTHER';
    surveys_3d?: Survey3D[];
    structures_3d?: Structure3D[];
    assay_composites_3d?: AssayComposite3D[];
    assay_elements_3d?: AssayElement3D[];
    significant_intersections_3d?: SignificantIntersection3D[];
    structures_visual_3d?: StructureVisual3D[];
    commodity_samples_3d?: CommoditySample3D[];
    commodity_keys_3d?: CommodityKey3D[];
    survey_holes_downsampled?: number;
    empty: boolean;
    truncation?: WorkspaceTruncation;
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

type Mode = 'map' | 'section' | '3d' | 'structure' | 'logs' | 'compare';

const MODES: readonly Mode[] = ['map', 'section', '3d', 'structure', 'logs', 'compare'] as const;

/**
 * Initial mode from ?mode= on the URL.
 *
 * 2026-08-19 — the standalone /projects/{slug}/map and
 * /projects/{slug}/compare pages were deleted; both now 302 here, compare
 * carrying ?mode=compare. Without this the redirect would silently land the
 * user on MAP and look like the Compare feature had been dropped rather
 * than moved. Anything unrecognised falls back to 'map'.
 */
function initialMode(): Mode {
    if (typeof window === 'undefined') return 'map';
    const requested = new URLSearchParams(window.location.search).get('mode');
    return MODES.includes(requested as Mode) ? (requested as Mode) : 'map';
}
type Tool = 'pan' | 'draw' | 'measure' | 'select';

/** Shared empty value for deferred props that have not arrived yet (stable identity for memo deps). */
const EMPTY: never[] = [];

/**
 * Placeholder for panels fed by the deferred 3D group. A deferred prop with
 * no placeholder reads as a broken page.
 */
function DeferredPanelSkeleton({ label }: { label: string }) {
    return (
        <div
            role="status"
            aria-live="polite"
            data-testid="deferred-skeleton"
            className="flex-1 flex flex-col gap-3 min-h-[240px] p-4 rounded border animate-pulse"
            style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}
        >
            <div className="h-3 w-48 rounded" style={{ background: 'var(--bg-3, var(--bg-2))' }} />
            <div className="h-3 w-80 rounded" style={{ background: 'var(--bg-3, var(--bg-2))' }} />
            <div className="flex-1 rounded" style={{ background: 'var(--bg-2)' }} />
            <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                {label}
            </span>
        </div>
    );
}

export default function FoundryWorkspace({
    project,
    project_extent = null,
    project_summary,
    project_aoi,
    collars,
    sections_count,
    intervals_count,
    structures_count,
    structures_visual_count,
    well_log_curves_count,
    curve_summary,
    log_tracks,
    log_available_curves,
    log_selected_curves,
    log_curves_max,
    log_hole_id,
    log_depth_max,
    log_hole_options,
    log_hole_total_depth,
    log_hole_easting,
    log_hole_northing,
    log_lithology_intervals,
    log_alteration_intervals = [],
    log_mineralization_intervals = [],
    log_tracks_truncated,
    first_holes_intervals = EMPTY,
    project_layers,
    strat_units,
    strat_source,
    project_country,
    surveys_3d = EMPTY,
    structures_3d = EMPTY,
    assay_composites_3d = EMPTY,
    assay_elements_3d = EMPTY,
    significant_intersections_3d = EMPTY,
    structures_visual_3d = EMPTY,
    commodity_samples_3d = EMPTY,
    commodity_keys_3d = EMPTY,
    survey_holes_downsampled,
    empty,
    truncation,
}: WorkspaceProps) {
    // Real-time push, scoped (FE-11). This used to router.reload() every
    // prop — the whole multi-MB 3D payload included — on any event carrying
    // `reports`, which every ingest completion carries. reloadPlan() maps
    // each affected type to the props that actually read it; the deferred
    // 3D group is refetched now if a 3D-using mode is on screen, otherwise
    // on the next visit to one.
    const modeRef = useRef<Mode>(initialMode());
    const viz3dStaleRef = useRef(false);
    useWorkspaceDataUpdated(project.project_id, (event) => {
        const plan = reloadPlan(event.affected_types);
        if (plan.props === 'all') {
            viz3dStaleRef.current = false;
            router.reload();
            return;
        }
        const only = [...plan.props];
        if (plan.viz3d) {
            if (modeRef.current === '3d' || modeRef.current === 'structure') {
                only.push(...VIZ3D_PROPS);
            } else {
                viz3dStaleRef.current = true;
            }
        }
        if (only.length > 0) {
            router.reload({ only });
        }
    });

    // STRUCTURE-panel inputs.
    //
    // The panel used to render `<StereonetMini measurements={[]} />` under a
    // header reading "STEREONET · {n} measurements", with a line of
    // developer prose underneath saying the arrays were "not yet emitted by
    // WorkspaceController". They were emitted — as `structures_3d` and
    // `structures_visual_3d`, under names the panel did not look for — so a
    // geologist with 47 structural readings saw the count, an empty net,
    // and a note addressed to someone else.
    //
    // Both sources are used, not one: silver.structure holds logged
    // planar attitudes, gold.structure_measurements_visual holds the
    // derived stereonet-ready set, and a project can have either.
    // The conversion itself lives in lib/structureProjection with its
    // tests — a pole that is 90° out still looks like structural data.
    const poles = useMemo<StereonetPole[]>(
        () => structurePoles(structures_3d, structures_visual_3d),
        [structures_3d, structures_visual_3d],
    );

    const strikes = useMemo<number[]>(
        () => structureStrikes(structures_3d, structures_visual_3d),
        [structures_3d, structures_visual_3d],
    );

    const [mode, setMode] = useState<Mode>(initialMode);
    // 3D opens on a sub-view that HAS something in it.
    //
    // It used to always open on 'lithology', which draws
    // gold.drillhole_intervals_visual. A project with drill holes but no
    // logged lithology — a historic delivery of collars, surveys and scans,
    // which is what most first uploads are — therefore opened 3D on an
    // empty canvas while TRAJECTORIES sat one segment away with every hole
    // in it. The mode looked broken at exactly the moment it had data to
    // show. Order below is "richest first": whichever is populated wins,
    // and lithology stays the preference when it is.
    //
    // Judged on the EAGER counts: the 3D arrays arrive deferred, after mount.
    // An only-gold-structures project opens on Structure Discs (the view that
    // draws that table), not an empty Stereosphere (FE-25).
    const [view3d, setView3d] = useState<View3D>(() =>
        initialView3D({
            intervalsCount: intervals_count,
            collarsCount: collars.length,
            structuresCount: structures_count,
            structuresVisualCount: structures_visual_count,
        }),
    );
    const [tool, setTool] = useState<Tool>('pan');
    const [projectLayersOn, setProjectLayersOn] = useState<Record<string, boolean>>(() =>
        Object.fromEntries(project_layers.map((l) => [l.id, l.on])),
    );
    const [copilotOpen, setCopilotOpen] = useState(true);
    const [copilotPrompt, setCopilotPrompt] = useState('');
    const [compareSet, setCompareSet] = useState<string[]>([]);
    const [compareOpen, setCompareOpen] = useState(false);
    // COMPARE mode's own left/right selection. Deliberately separate from
    // `compareSet` above: that one is the map's "queue two holes by clicking
    // pins" flow which auto-opens the modal, this one is an explicit
    // two-dropdown pick inside the panel. Sharing a single state would make
    // picking a hole in the panel pop the modal open over top of it.
    const [compareLeft, setCompareLeft] = useState<string>('');
    const [compareRight, setCompareRight] = useState<string>('');
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
    const [visitedModes, setVisitedModes] = useState<Set<Mode>>(() => new Set<Mode>([initialMode()]));
    // Entering a 3D-using mode after an ingest marked the 3D group stale.
    useEffect(() => {
        modeRef.current = mode;
        if ((mode === '3d' || mode === 'structure') && viz3dStaleRef.current) {
            viz3dStaleRef.current = false;
            router.reload({ only: [...VIZ3D_PROPS] });
        }
    }, [mode]);
    const [showReferenceStrat, setShowReferenceStrat] = useState(false);
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
        // Only MAP (and COMPARE, which has its own message) can show anything
        // for a project with GIS data but no drill holes.
        if (empty && target !== 'map' && target !== 'compare') {
            content = (
                <EmptyState
                    title="No drill holes in this project yet."
                    detail="This mode draws collars, surveys and downhole data. The project's map layers are in MAP mode; add drill data via Data → Connect Source."
                />
            );
        }

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

    const terrainNotice = describeTerrainElevation(collars);
    const truncationNotices = [
        ...describeTruncation(
            truncation ? { ...truncation, survey_holes_downsampled: survey_holes_downsampled ?? 0 } : truncation,
        ),
        ...(terrainNotice ? [terrainNotice] : []),
    ];

    // FE-3: the canvas is shown when the project has ANY map data. It used to
    // be gated on collars alone, so a delivery of shapefiles / geochem /
    // claims — already counted in the Layers rail — had nowhere to be seen.
    const hasMapData = !empty || hasNonCollarMapData(project_layers);
    const logCollar = log_hole_id ? findCollar(log_hole_id) : null;

    // A hole can have a logged strip with no curves at all (a geology log and no LAS).
    const hasLogGeologyTracks = log_alteration_intervals.length > 0 || log_mineralization_intervals.length > 0;
    const hasLogGeology = log_lithology_intervals.length > 0 || hasLogGeologyTracks;
    // ONE depth axis for the curve tracks and the geology column drawn beside
    // them. `log_depth_max` is the deepest drawn CURVE — or the API's 600 m
    // placeholder when no curve is drawn, which must not become the axis of a
    // geology-only hole — and the geology can run deeper than the curves, so
    // the axis is the deeper of the two. The column ignored it altogether
    // before and fitted its own data, so the two tracks disagreed on depth.
    const logDepthAxis = sharedDepthAxis([
        log_tracks.length > 0 ? log_depth_max : null,
        geologyDepth({
            intervals: log_lithology_intervals,
            alteration: log_alteration_intervals,
            mineralization: log_mineralization_intervals,
        }),
    ]);

    function changeLogCurves(next: string[]) {
        router.get(
            `/projects/${project.slug}/workspace`,
            { log_hole: log_hole_id ?? undefined, log_curves: next.join(',') },
            {
                preserveScroll: true,
                preserveState: true,
                only: [
                    'log_tracks',
                    'log_available_curves',
                    'log_selected_curves',
                    'log_hole_id',
                    'log_depth_max',
                    'log_hole_total_depth',
                    'log_hole_easting',
                    'log_hole_northing',
                    'log_lithology_intervals',
                    'log_alteration_intervals',
                    'log_mineralization_intervals',
                    'log_tracks_truncated',
                    'log_hole_options',
                ],
            },
        );
    }

    return (
        <>
            <Head title={`Workspace · ${project.project_name}`} />

            <div
                className="flex-1 flex flex-col overflow-hidden"
                style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}
            >
                {!isCanvasFullscreen && (
                    <PageHeader
                        eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · WORKSPACE`}
                        title="Project canvas"
                        sub={`${truncation?.collars?.truncated ? `${collars.length} of ${truncation.collars.total}` : collars.length} collars · ${well_log_curves_count} log curves · ${sections_count} section panels · ${structures_count} structures`}
                    />
                )}

                {/* Toolbar — mode + tool segments, side-by-side per the V2 layout.
                    Hidden in fullscreen; mode switching from there requires
                    Esc (or the floating Exit button) first. */}
                {!isCanvasFullscreen && (
                    <div
                        className="flex items-center gap-3 px-8 py-2 border-b shrink-0"
                        style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)' }}
                    >
                        <span
                            className="text-[10px] font-mono uppercase tracking-widest"
                            style={{ color: 'var(--fg-3)' }}
                        >
                            Mode
                        </span>
                        {/* Shared with the Rasters page so both render one
                            row of modes. RASTERS is a URL, not a panel here —
                            see Components/Foundry/WorkspaceModeBar. */}
                        <WorkspaceModeBar
                            slug={project.slug}
                            active={mode}
                            onSelectInPage={(next) => setMode(next as Mode)}
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
                    </div>
                )}

                {!isCanvasFullscreen && truncationNotices.length > 0 && (
                    <div
                        role="status"
                        data-testid="workspace-truncation-notice"
                        className="px-8 py-1.5 border-b text-[11px] font-mono shrink-0"
                        style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)', color: 'var(--fg-2)' }}
                    >
                        {truncationNotices.join(' ')}
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
                        <div
                            className="px-3 py-3 border-b text-[10px] font-mono uppercase tracking-[0.12em]"
                            style={{ borderColor: 'var(--line-1)', color: 'var(--fg-3)' }}
                        >
                            Layers
                        </div>
                        <div className="px-3 py-2">
                            <div
                                className="text-[10px] font-mono uppercase tracking-wider mb-1.5"
                                style={{ color: 'var(--fg-3)' }}
                            >
                                Project
                            </div>
                            {project_layers.map((layer) => {
                                const has = layer.count > 0;
                                const checked = projectLayersOn[layer.id] ?? false;
                                return (
                                    <label
                                        key={layer.id}
                                        className={`flex items-center gap-2 py-1 text-xs ${has ? 'cursor-pointer' : 'cursor-default'}`}
                                    >
                                        <input
                                            type="checkbox"
                                            checked={checked}
                                            disabled={!has}
                                            onChange={(e) =>
                                                setProjectLayersOn({ ...projectLayersOn, [layer.id]: e.target.checked })
                                            }
                                        />
                                        <span style={{ color: has ? 'var(--fg-1)' : 'var(--fg-3)' }}>
                                            {layer.label}
                                        </span>
                                        <span
                                            className="ml-auto text-[10px] font-mono"
                                            style={{ color: has ? 'var(--fg-2)' : 'var(--fg-3)' }}
                                        >
                                            {layer.count.toLocaleString()}
                                        </span>
                                    </label>
                                );
                            })}
                        </div>
                    </aside>

                    {/* Mode canvas — flex-col so map/charts can fill viewport.
                        Internal scroll lives on the chart card content,
                        not on the section, so the page never grows past 100vh. */}
                    <section className={`flex flex-col overflow-hidden min-h-0${isCanvasFullscreen ? ' p-0' : ' p-6'}`}>
                        {!hasMapData ? (
                            <EmptyState
                                title="Nothing to show in this project yet."
                                detail="Upload drill data (collars, surveys, logs) or map layers (shapefiles, GeoPackage, geochemistry, claims) via Data → Connect Source to populate the workspace canvases."
                                action={
                                    <Link
                                        href={`/projects/${project.slug}/reports`}
                                        className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                        style={{
                                            color: 'var(--accent)',
                                            background: 'var(--accent-bg)',
                                            borderColor: 'var(--accent-dim)',
                                        }}
                                    >
                                        Open import quality →
                                    </Link>
                                }
                            />
                        ) : (
                            <>
                                {renderModePanel(
                                    'map',
                                    <Card
                                        eyebrow={`MAP · MAPLIBRE · ${collars.length} COLLARS`}
                                        title="Project collars on basemap"
                                        className="flex-1 flex flex-col min-h-0"
                                        contentClassName="flex-1 flex flex-col min-h-0"
                                    >
                                        <div
                                            className="text-[10px] font-mono mb-2 shrink-0"
                                            style={{ color: 'var(--fg-3)' }}
                                        >
                                            Click any collar for detail + jump to LOGS. Hover for tooltip. Layer toggles
                                            (left rail): "Collars" hides dots · "Ore-bearing holes only" filters to
                                            ore-bearing holes · "Ore heatmap" turns on the thickness heatmap.
                                        </div>
                                        <div className="flex-1 min-h-0">
                                            <WorkspaceMap
                                                collars={collars}
                                                projectSlug={project.slug}
                                                // The MVT tile URL keys on the UUID,
                                                // not the slug: /tiles/silver/{fn}/
                                                // {z}/{x}/{y}.pbf?project_id={uuid}
                                                projectId={project.project_id}
                                                dataVersion={project.data_version ?? 0}
                                                projectExtent={project_extent}
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
                                                            only: [...LOG_PROPS],
                                                        },
                                                    );
                                                }}
                                            />
                                        </div>
                                    </Card>,
                                )}
                                {renderModePanel(
                                    'section',
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
                                                detail="This project has fewer than 2 collars with well-log curves. Ingest more LAS files via Data → Connect Source."
                                            />
                                        )}
                                    </Card>,
                                )}
                                {renderModePanel(
                                    '3d',
                                    <Deferred
                                        data={[...VIZ3D_PROPS]}
                                        fallback={<DeferredPanelSkeleton label="Loading 3D data…" />}
                                    >
                                        {(() => {
                                            // Resolve the "active hole" for the per-hole 3D sub-views
                                            // (Spiral). Prefer the LOGS panel's current hole if set,
                                            // otherwise fall back to the first collar with usable
                                            // azimuth/dip on the project.
                                            const spiralCollar = (() => {
                                                const target = log_hole_id;
                                                const match = collars.find((c) =>
                                                    target
                                                        ? c.hole_id_canonical === target || c.hole_id === target
                                                        : false,
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
                                                            const hid = spiralCollar
                                                                ? spiralCollar.hole_id_canonical || spiralCollar.hole_id
                                                                : '—';
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
                                                        if (view3d === 'stereosphere')
                                                            return '3D stereosphere · lower hemisphere';
                                                        if (view3d === 'spiral')
                                                            return 'Per-hole 3D orientation spiral';
                                                        if (view3d === 'project_stereonet')
                                                            return 'Project-wide aggregate stereonet (2D + 3D)';
                                                        if (view3d === 'assay_grade')
                                                            return 'Assay composites · grade-coloured sticks';
                                                        if (view3d === 'significant_intersections')
                                                            return 'Significant cutoff-grade intersections';
                                                        if (view3d === 'structure_discs')
                                                            return 'Structure measurements · oriented discs in space';
                                                        return 'Commodity grade samples';
                                                    })()}
                                                    actions={
                                                        <Segmented<View3D>
                                                            value={view3d}
                                                            onChange={setView3d}
                                                            options={[
                                                                { value: 'lithology', label: 'Lithology' },
                                                                { value: 'trajectories', label: 'Trajectories' },
                                                                { value: 'spiral', label: 'Spiral' },
                                                                { value: 'stereosphere', label: 'Stereosphere' },
                                                                {
                                                                    value: 'project_stereonet',
                                                                    label: 'Project Stereonet',
                                                                },
                                                                { value: 'assay_grade', label: 'Assay Grade' },
                                                                {
                                                                    value: 'significant_intersections',
                                                                    label: 'Intersections',
                                                                },
                                                                { value: 'structure_discs', label: 'Structure Discs' },
                                                                {
                                                                    value: 'commodity_samples',
                                                                    label: 'Commodity Samples',
                                                                },
                                                            ]}
                                                        />
                                                    }
                                                    className="flex-1 flex flex-col min-h-0"
                                                    contentClassName="flex-1 flex flex-col min-h-0"
                                                >
                                                    {view3d === 'lithology' &&
                                                        (intervals_count > 0 ? (
                                                            <>
                                                                <div
                                                                    className="text-[11px] font-mono mb-3 shrink-0"
                                                                    style={{ color: 'var(--fg-3)' }}
                                                                >
                                                                    Each hole drawn along its desurveyed path (surveys,
                                                                    or collar azimuth/dip when it has none) and coloured
                                                                    by derived lithology bands. Drag to rotate, scroll
                                                                    to zoom, shift-drag to pan. Hover a band for hole ID
                                                                    / depth interval / lithology code.
                                                                </div>
                                                                <div className="flex-1 min-h-0">
                                                                    <Suspense
                                                                        fallback={
                                                                            <EmptyState
                                                                                title="Loading 3D viewer…"
                                                                                detail=""
                                                                            />
                                                                        }
                                                                    >
                                                                        <Borehole3DView
                                                                            holes={first_holes_intervals}
                                                                            collars={collars}
                                                                            surveys={surveys_3d}
                                                                            height={chartH}
                                                                        />
                                                                    </Suspense>
                                                                </div>
                                                            </>
                                                        ) : (
                                                            <EmptyState
                                                                title="No 3D intervals for this project yet."
                                                                detail="3D intervals are built from well-log curves. If this project has curves but no intervals, they have not been computed yet — they appear once the curves have been processed."
                                                            />
                                                        ))}
                                                    {view3d === 'trajectories' &&
                                                        (collars.length > 0 ? (
                                                            <>
                                                                <div
                                                                    className="text-[11px] font-mono mb-3 shrink-0"
                                                                    style={{ color: 'var(--fg-3)' }}
                                                                >
                                                                    Every drill hole desurveyed from its collar (minimum
                                                                    curvature) and extended to TD. Dashed = no downhole
                                                                    survey, projected along the collar azimuth/dip.
                                                                    Colour-coded by hole status — green = completed,
                                                                    amber = active, red = abandoned.
                                                                </div>
                                                                <div className="flex-1 min-h-0">
                                                                    <Suspense
                                                                        fallback={
                                                                            <EmptyState
                                                                                title="Loading 3D trajectories…"
                                                                                detail=""
                                                                            />
                                                                        }
                                                                    >
                                                                        <MultiHole3DTrace
                                                                            collars={collars.map((c) => ({
                                                                                collar_id: c.collar_id,
                                                                                hole_id:
                                                                                    c.hole_id_canonical || c.hole_id,
                                                                                azimuth: c.azimuth ?? null,
                                                                                dip: c.dip ?? null,
                                                                                elevation: c.elevation ?? null,
                                                                                easting: c.easting,
                                                                                northing: c.northing,
                                                                                total_depth: c.total_depth,
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
                                                        ))}
                                                    {view3d === 'stereosphere' &&
                                                        (structures_3d.length > 0 ? (
                                                            <>
                                                                <div
                                                                    className="text-[11px] font-mono mb-3 shrink-0"
                                                                    style={{ color: 'var(--fg-3)' }}
                                                                >
                                                                    Planar measurements rendered as great-circle arcs on
                                                                    the lower hemisphere; lineations as point cloud.
                                                                    Colour-coded by structure type. Drag to rotate,
                                                                    scroll to zoom — read structural geometry directly
                                                                    rather than through a 2-D equal-area projection.
                                                                </div>
                                                                <div className="flex-1 min-h-0">
                                                                    <Suspense
                                                                        fallback={
                                                                            <EmptyState
                                                                                title="Loading 3D stereosphere…"
                                                                                detail=""
                                                                            />
                                                                        }
                                                                    >
                                                                        <Stereosphere
                                                                            structures={structures_3d}
                                                                            holeId={`project-${project.slug}`}
                                                                        />
                                                                    </Suspense>
                                                                </div>
                                                            </>
                                                        ) : (
                                                            <EmptyState
                                                                title="No logged planar structures in this project."
                                                                detail={
                                                                    structures_visual_3d.length > 0
                                                                        ? 'The stereosphere draws logged structure rows (dip + dip direction). This project has derived structure measurements instead — see Structure Discs.'
                                                                        : 'The stereosphere needs logged planar features (bedding, foliation, joints, faults, veins) with a dip and a dip direction. Upload a structure table (hole, depth, dip, dip direction) via Data → Connect Source.'
                                                                }
                                                            />
                                                        ))}
                                                    {view3d === 'spiral' &&
                                                        (spiralCollar &&
                                                        (spiralSurveys.length > 0 ||
                                                            (spiralCollar.azimuth != null &&
                                                                spiralCollar.dip != null)) ? (
                                                            <>
                                                                <div
                                                                    className="text-[11px] font-mono mb-3 shrink-0"
                                                                    style={{ color: 'var(--fg-3)' }}
                                                                >
                                                                    Active hole's deviation surveys integrated into a
                                                                    3-D minimum-curvature spiral. Hole picked from the
                                                                    LOGS panel (or first collar by default). Useful for
                                                                    spotting survey drift, dogleg severity, and how far
                                                                    the bit walked from its planned path.
                                                                </div>
                                                                <div className="flex-1 min-h-0">
                                                                    <Suspense
                                                                        fallback={
                                                                            <EmptyState
                                                                                title="Loading orientation spiral…"
                                                                                detail=""
                                                                            />
                                                                        }
                                                                    >
                                                                        <OrientationSpiral
                                                                            surveys={spiralSurveys}
                                                                            collarAzimuth={spiralCollar.azimuth ?? null}
                                                                            collarDip={spiralCollar.dip ?? null}
                                                                            collarElevation={
                                                                                spiralCollar.elevation ?? null
                                                                            }
                                                                            totalDepth={
                                                                                spiralCollar.total_depth ?? null
                                                                            }
                                                                            view="3d"
                                                                        />
                                                                    </Suspense>
                                                                </div>
                                                            </>
                                                        ) : (
                                                            <EmptyState
                                                                title="Not enough survey data for an orientation spiral."
                                                                detail="Needs downhole survey stations for the active hole, or a collar azimuth + dip. Upload a survey table (hole, depth, azimuth, dip) via Data → Connect Source."
                                                            />
                                                        ))}
                                                    {view3d === 'project_stereonet' &&
                                                        (structures_3d.length > 0 ? (
                                                            <>
                                                                <div
                                                                    className="text-[11px] font-mono mb-3 shrink-0"
                                                                    style={{ color: 'var(--fg-3)' }}
                                                                >
                                                                    Project-wide aggregate of every structural
                                                                    measurement across every hole. Toggle 2D/3D with the
                                                                    dimension switch on the left. Filter by structure
                                                                    type to isolate bedding, foliation, joints, faults,
                                                                    shears, veins, or lineations.
                                                                </div>
                                                                <div className="flex-1 min-h-0 overflow-auto">
                                                                    <Suspense
                                                                        fallback={
                                                                            <EmptyState
                                                                                title="Loading project stereonet…"
                                                                                detail=""
                                                                            />
                                                                        }
                                                                    >
                                                                        <AggregateStereonet
                                                                            structures={structures_3d}
                                                                        />
                                                                    </Suspense>
                                                                </div>
                                                            </>
                                                        ) : (
                                                            <EmptyState
                                                                title="No logged planar structures in this project."
                                                                detail="The aggregate stereonet combines logged planar features (bedding, foliation, joints, faults) across every hole in this project. Upload a structure table via Data → Connect Source."
                                                            />
                                                        ))}
                                                    {view3d === 'assay_grade' &&
                                                        (assay_elements_3d.length > 0 ? (
                                                            <>
                                                                <div
                                                                    className="text-[11px] font-mono mb-3 shrink-0"
                                                                    style={{ color: 'var(--fg-3)' }}
                                                                >
                                                                    Composited assay grades for this project. Each band
                                                                    on each hole is coloured by the composite's
                                                                    weighted-average grade for the selected element.
                                                                    Compare with the Lithology view — Lithology shows
                                                                    derived rock type, this shows real assayed grade.
                                                                </div>
                                                                <div className="flex-1 min-h-0">
                                                                    <Suspense
                                                                        fallback={
                                                                            <EmptyState
                                                                                title="Loading assay composites…"
                                                                                detail=""
                                                                            />
                                                                        }
                                                                    >
                                                                        <AssayComposites3DView
                                                                            collars={collars}
                                                                            surveys={surveys_3d}
                                                                            composites={assay_composites_3d}
                                                                            elements={assay_elements_3d}
                                                                            height={chartH}
                                                                        />
                                                                    </Suspense>
                                                                </div>
                                                            </>
                                                        ) : (
                                                            <EmptyState
                                                                title="No assay composites yet."
                                                                detail="Assay composites are computed from this project's assays at common cutoff grades. Upload assay data via Data → Connect Source and they will appear once the assays have been processed."
                                                            />
                                                        ))}
                                                    {view3d === 'significant_intersections' &&
                                                        (significant_intersections_3d.length > 0 ? (
                                                            <>
                                                                <div
                                                                    className="text-[11px] font-mono mb-3 shrink-0"
                                                                    style={{ color: 'var(--fg-3)' }}
                                                                >
                                                                    Cutoff-grade hits for this project. Ghost-rendered
                                                                    hole sticks with each significant interval glowing
                                                                    in heat-palette colour by weighted-average grade.
                                                                    White marker = peak grade depth. Use it to spot
                                                                    which holes hit ore-grade mineralisation and where.
                                                                </div>
                                                                <div className="flex-1 min-h-0">
                                                                    <Suspense
                                                                        fallback={
                                                                            <EmptyState
                                                                                title="Loading significant intersections…"
                                                                                detail=""
                                                                            />
                                                                        }
                                                                    >
                                                                        <SignificantIntersections3DView
                                                                            collars={collars}
                                                                            surveys={surveys_3d}
                                                                            intersections={significant_intersections_3d}
                                                                            height={chartH}
                                                                        />
                                                                    </Suspense>
                                                                </div>
                                                            </>
                                                        ) : (
                                                            <EmptyState
                                                                title="No significant intersections yet."
                                                                detail="Significant intersections are computed from this project's assays at cutoff grades. Once assays have been imported and processed, every cutoff-grade hit per hole shows up here as a highlight ribbon."
                                                            />
                                                        ))}
                                                    {view3d === 'commodity_samples' &&
                                                        (commodity_keys_3d.length > 0 ? (
                                                            <>
                                                                <div
                                                                    className="text-[11px] font-mono mb-3 shrink-0"
                                                                    style={{ color: 'var(--fg-3)' }}
                                                                >
                                                                    Commodity grades per sample interval, placed along
                                                                    each hole's desurveyed path. Pick a commodity to see
                                                                    grade variation along every hole.
                                                                </div>
                                                                <div className="flex-1 min-h-0">
                                                                    <Suspense
                                                                        fallback={
                                                                            <EmptyState
                                                                                title="Loading commodity samples…"
                                                                                detail=""
                                                                            />
                                                                        }
                                                                    >
                                                                        <CommoditySamples3DView
                                                                            collars={collars}
                                                                            surveys={surveys_3d}
                                                                            samples={commodity_samples_3d}
                                                                            commodityKeys={commodity_keys_3d}
                                                                            height={chartH}
                                                                        />
                                                                    </Suspense>
                                                                </div>
                                                            </>
                                                        ) : (
                                                            <EmptyState
                                                                title="No commodity samples for this project yet."
                                                                detail="Commodity samples hold the grade for each sampled interval (for example U3O8 % or Au g/t). Import a CSV or QGIS assay sample file via Data → Connect Source to see them here."
                                                            />
                                                        ))}
                                                    {view3d === 'structure_discs' &&
                                                        (structures_visual_3d.length > 0 ? (
                                                            <>
                                                                <div
                                                                    className="text-[11px] font-mono mb-3 shrink-0"
                                                                    style={{ color: 'var(--fg-3)' }}
                                                                >
                                                                    Each structural measurement is rendered as an
                                                                    oriented disc in the plane perpendicular to its
                                                                    pole, positioned at the measurement depth on its
                                                                    collar. Different from Stereosphere — that abstracts
                                                                    onto a unit sphere; this anchors in real space so
                                                                    spatial clustering is visible.
                                                                </div>
                                                                <div className="flex-1 min-h-0">
                                                                    <Suspense
                                                                        fallback={
                                                                            <EmptyState
                                                                                title="Loading structure discs…"
                                                                                detail=""
                                                                            />
                                                                        }
                                                                    >
                                                                        <StructureDiscs3DView
                                                                            collars={collars}
                                                                            surveys={surveys_3d}
                                                                            structures={structures_visual_3d}
                                                                            height={chartH}
                                                                        />
                                                                    </Suspense>
                                                                </div>
                                                            </>
                                                        ) : (
                                                            <EmptyState
                                                                title="No structural measurements to display in 3D yet."
                                                                detail="Oriented discs need structural measurements that have been processed for 3D. Until then, the Stereosphere and Project Stereonet views still work from the logged structure data."
                                                            />
                                                        ))}
                                                </Card>
                                            );
                                        })()}
                                    </Deferred>,
                                )}
                                {renderModePanel(
                                    'structure',
                                    <Deferred
                                        data={['structures_3d', 'structures_visual_3d']}
                                        fallback={<DeferredPanelSkeleton label="Loading structure measurements…" />}
                                    >
                                        {structures_count > 0 || structures_visual_count > 0 ? (
                                            <div className="grid grid-cols-2 gap-4">
                                                <Card
                                                    eyebrow={`STEREONET · ${poles.length} poles`}
                                                    title="Schmidt equal-area"
                                                >
                                                    <StereonetMini poles={poles} size={260} />
                                                    <div
                                                        className="text-[10px] font-mono mt-2"
                                                        style={{ color: 'var(--fg-3)' }}
                                                    >
                                                        {poles.length > 0
                                                            ? 'Poles to planes, lower hemisphere. North up.'
                                                            : `${structures_count + structures_visual_count} structure row(s) recorded, none carrying both a dip and a dip direction — nothing to project.`}
                                                    </div>
                                                </Card>
                                                <Card
                                                    eyebrow={`ROSE DIAGRAM · ${strikes.length} strikes`}
                                                    title="Strike frequency"
                                                >
                                                    <RoseMini strikes={strikes} size={260} />
                                                    <div
                                                        className="text-[10px] font-mono mt-2"
                                                        style={{ color: 'var(--fg-3)' }}
                                                    >
                                                        {strikes.length > 0
                                                            ? '10° bins, radius proportional to count.'
                                                            : 'No dip directions recorded, so no strikes to bin.'}
                                                    </div>
                                                </Card>
                                            </div>
                                        ) : (
                                            <Card eyebrow="STRUCTURE" title="No structure measurements yet">
                                                {/* Was: "0 rows in silver.structures +
                                                gold.structure_measurements_visual" — two table
                                                names, one of them wrong (the table is
                                                silver.structure, singular), addressed to nobody
                                                who reads this screen. A geologist needs to know
                                                what to upload. */}
                                                <EmptyState
                                                    title="Nothing to plot on a stereonet yet."
                                                    detail="Downhole surveys give this project hole orientation, but a stereonet needs logged planar readings — joint, foliation, fault or bedding measurements with a dip and a dip direction. Upload a structure table, or a shapefile of structural readings, and this panel fills in."
                                                />
                                            </Card>
                                        )}
                                    </Deferred>,
                                )}
                                {renderModePanel(
                                    'logs',
                                    <Card
                                        eyebrow={log_hole_id ? `LOGS · HOLE ${log_hole_id}` : 'LOGS'}
                                        title={
                                            log_tracks.length > 0
                                                ? `${log_tracks.length} of ${log_available_curves.length} curves rendered · ${well_log_curves_count} total in project`
                                                : hasLogGeology
                                                  ? `${log_lithology_intervals.length} lithology · ${log_alteration_intervals.length} alteration · ${log_mineralization_intervals.length} mineralization · no curves`
                                                  : 'No curve data'
                                        }
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
                                        <LogCurveToggles
                                            available={log_available_curves}
                                            selected={log_selected_curves}
                                            max={log_curves_max}
                                            onChange={changeLogCurves}
                                        />
                                        {log_tracks.length > 0 || hasLogGeology ? (
                                            <>
                                                {log_tracks.length > 0 && (
                                                    <div
                                                        className="text-[11px] font-mono mb-3 shrink-0"
                                                        style={{ color: 'var(--fg-3)' }}
                                                    >
                                                        Curves available across project:{' '}
                                                        {curve_summary
                                                            .map((c) => `${c.curve_name} (${c.curves})`)
                                                            .join(' · ')}
                                                    </div>
                                                )}
                                                <div className="flex gap-6 overflow-auto items-start flex-1 min-h-0 py-1 px-1">
                                                    {log_tracks.length > 0 && (
                                                        <div className="shrink-0">
                                                            <DownholeMultiLog
                                                                tracks={log_tracks}
                                                                depthMax={logDepthAxis}
                                                                height={chartH}
                                                                trackWidth={96}
                                                            />
                                                        </div>
                                                    )}
                                                    <div className="shrink-0">
                                                        <LithologyStripColumn
                                                            intervals={log_lithology_intervals}
                                                            alteration={log_alteration_intervals}
                                                            mineralization={log_mineralization_intervals}
                                                            truncated={log_tracks_truncated}
                                                            holeId={log_hole_id}
                                                            depthMax={logDepthAxis}
                                                            height={chartH}
                                                            width={hasLogGeologyTracks ? 520 : 380}
                                                        />
                                                    </div>
                                                    <div
                                                        className="shrink-0 flex flex-col gap-3"
                                                        style={{ width: 420 }}
                                                    >
                                                        <div
                                                            className="text-[11px] font-mono px-4 py-3 rounded border"
                                                            style={{
                                                                borderColor: 'var(--line-1)',
                                                                background: 'var(--bg-2)',
                                                                color: 'var(--fg-2)',
                                                            }}
                                                        >
                                                            <div
                                                                className="uppercase tracking-wider mb-1"
                                                                style={{ color: 'var(--fg-3)' }}
                                                            >
                                                                Hole context
                                                            </div>
                                                            <div className="text-sm" style={{ color: 'var(--fg-0)' }}>
                                                                {log_hole_id ?? '—'}
                                                                {log_hole_total_depth !== null && (
                                                                    <span style={{ color: 'var(--fg-2)' }}>
                                                                        {' '}
                                                                        · TD {log_hole_total_depth.toFixed(1)} m
                                                                    </span>
                                                                )}
                                                            </div>
                                                            {log_hole_easting !== null &&
                                                                log_hole_northing !== null && (
                                                                    <div
                                                                        className="mt-1.5"
                                                                        style={{ color: 'var(--fg-3)' }}
                                                                    >
                                                                        {/* FE-18: was a hard-coded "UTM 13N" on every project. */}
                                                                        {crsLabel(project.crs_epsg)} · E{' '}
                                                                        {Math.round(log_hole_easting).toLocaleString()}{' '}
                                                                        · N{' '}
                                                                        {Math.round(log_hole_northing).toLocaleString()}
                                                                    </div>
                                                                )}
                                                            {logCollar && (
                                                                // FE-15: the per-hole page had no inbound link.
                                                                <Link
                                                                    href={`/projects/${project.slug}/holes/${encodeURIComponent(logCollar.collar_id)}/detail`}
                                                                    className="inline-block mt-2 text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border"
                                                                    style={{
                                                                        color: 'var(--accent)',
                                                                        borderColor: 'var(--accent-dim)',
                                                                        background: 'var(--accent-bg)',
                                                                    }}
                                                                >
                                                                    Open hole page →
                                                                </Link>
                                                            )}
                                                        </div>
                                                        {strat_source === 'project' || showReferenceStrat ? (
                                                            <ChronoColumn
                                                                units={strat_units}
                                                                height={Math.max(360, chartH - 100)}
                                                                width={420}
                                                                eyebrow={
                                                                    strat_source === 'project'
                                                                        ? 'Project chronostratigraphy'
                                                                        : `Regional reference — NOT this project's stratigraphy · ${project_country === 'US' ? 'Wyoming roll-front uranium' : 'Athabasca / Wollaston Domain'}`
                                                                }
                                                                title={
                                                                    strat_source === 'project'
                                                                        ? 'Stratigraphic column'
                                                                        : project_country === 'US'
                                                                          ? 'Shirley / PRB / WRB roll-front host stack'
                                                                          : 'Athabasca Group · Wollaston Domain'
                                                                }
                                                            />
                                                        ) : (
                                                            // FE-25: a regional column (Athabasca, or a Wyoming
                                                            // roll-front stack) was shown for every project in
                                                            // that country whatever its geology. Now opt-in and
                                                            // labelled; which column, if any, is right for a
                                                            // project is an SME call.
                                                            <div
                                                                className="text-[11px] font-mono px-4 py-3 rounded border"
                                                                style={{
                                                                    borderColor: 'var(--line-1)',
                                                                    background: 'var(--bg-2)',
                                                                    color: 'var(--fg-2)',
                                                                }}
                                                            >
                                                                <div
                                                                    className="uppercase tracking-wider mb-1"
                                                                    style={{ color: 'var(--fg-3)' }}
                                                                >
                                                                    Stratigraphic column
                                                                </div>
                                                                No formations are recorded for this project yet.
                                                                <button
                                                                    type="button"
                                                                    onClick={() => setShowReferenceStrat(true)}
                                                                    className="block mt-2 text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border"
                                                                    style={{
                                                                        color: 'var(--fg-2)',
                                                                        borderColor: 'var(--line-2)',
                                                                        background: 'var(--bg-1)',
                                                                    }}
                                                                >
                                                                    Show a regional reference column (
                                                                    {project_country === 'US'
                                                                        ? 'Wyoming roll-front'
                                                                        : 'Athabasca / Wollaston'}
                                                                    )
                                                                </button>
                                                            </div>
                                                        )}
                                                    </div>
                                                </div>
                                                {strat_source === 'reference' && showReferenceStrat && (
                                                    <div
                                                        className="text-[10px] font-mono mt-2 shrink-0"
                                                        style={{ color: 'var(--fg-3)' }}
                                                    >
                                                        Chrono column = regional reference, not derived from this
                                                        project (no formations recorded for it).
                                                    </div>
                                                )}
                                            </>
                                        ) : (
                                            <EmptyState
                                                title="No curves or logged intervals for this hole."
                                                detail="LOGS shows a hole's downhole curves (LAS) and its logged lithology, alteration and mineralization. Upload a LAS file, or a geology log with hole, from, to and lithology columns (alteration and mineral columns are read too), via Data → Connect Source."
                                            />
                                        )}
                                    </Card>,
                                )}

                                {/* COMPARE — absorbed 2026-08-19 from the standalone
                                    /projects/{slug}/compare page (HoleCompareController
                                    + Foundry/HoleCompare.tsx, both deleted). That page
                                    was a strictly weaker duplicate of machinery this
                                    surface already had: it hydrated collar metadata plus
                                    a plain-text lithology list server-side, with
                                    grade_avg / grade_top / rock_summary / intercepts
                                    hardcoded to null, while holePayload() +
                                    CompareHolesPanel render real log curves, colour-coded
                                    lithology bands, ore-band counts and mean grade.
                                    Nothing was ported — the weaker path was removed and
                                    the existing renderer given a picker. */}
                                {renderModePanel(
                                    'compare',
                                    <Card
                                        eyebrow="COMPARE · HOLE VS HOLE"
                                        title={
                                            compareLeft && compareRight
                                                ? `${compareLeft} vs ${compareRight}`
                                                : 'Pick two holes'
                                        }
                                        className="flex-1 flex flex-col min-h-0"
                                        contentClassName="flex-1 flex flex-col min-h-0"
                                    >
                                        {empty ? (
                                            <EmptyState
                                                title="No drill holes in this project yet."
                                                detail="Ingest at least two collars before this surface can compare them. Use Data → Connect Source to add drill logs."
                                            />
                                        ) : (
                                            <>
                                                <div className="flex items-center gap-3 mb-4 shrink-0">
                                                    <ComparePicker
                                                        label="LEFT"
                                                        value={compareLeft}
                                                        collars={collars}
                                                        onChange={setCompareLeft}
                                                    />
                                                    <span
                                                        className="text-[10px] font-mono uppercase tracking-wider"
                                                        style={{ color: 'var(--fg-3)' }}
                                                    >
                                                        vs
                                                    </span>
                                                    <ComparePicker
                                                        label="RIGHT"
                                                        value={compareRight}
                                                        collars={collars}
                                                        onChange={setCompareRight}
                                                    />
                                                    {collars.length >= 2 && !(compareLeft && compareRight) && (
                                                        <button
                                                            type="button"
                                                            onClick={() => {
                                                                setCompareLeft(
                                                                    collars[0].hole_id_canonical ?? collars[0].hole_id,
                                                                );
                                                                setCompareRight(
                                                                    collars[1].hole_id_canonical ?? collars[1].hole_id,
                                                                );
                                                            }}
                                                            className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border"
                                                            style={{
                                                                color: 'var(--fg-2)',
                                                                borderColor: 'var(--line-2)',
                                                                background: 'var(--bg-2)',
                                                            }}
                                                        >
                                                            Use first two
                                                        </button>
                                                    )}
                                                </div>
                                                <div className="flex-1 overflow-y-auto min-h-0">
                                                    {compareLeft && compareRight ? (
                                                        compareLeft === compareRight ? (
                                                            <EmptyState
                                                                title="Pick two different holes."
                                                                detail="Comparing a hole against itself renders two identical columns and no useful diff."
                                                            />
                                                        ) : (
                                                            <CompareHolesPanel
                                                                projectSlug={project.slug}
                                                                leftHole={compareLeft}
                                                                rightHole={compareRight}
                                                                chartHeight={Math.max(360, chartH - 120)}
                                                            />
                                                        )
                                                    ) : (
                                                        <EmptyState
                                                            title="Pick two holes to compare."
                                                            detail={`Choose any two of this project's ${collars.length} holes from the dropdowns above. You can also queue a pair by clicking two pins in MAP mode.`}
                                                        />
                                                    )}
                                                </div>
                                            </>
                                        )}
                                    </Card>,
                                )}
                            </>
                        )}
                    </section>

                    {/* Copilot dock */}
                    <aside
                        className={`border-l flex flex-col overflow-hidden${isCanvasFullscreen ? ' hidden' : ''}`}
                        style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}
                    >
                        <div className="px-3 py-3 border-b flex items-center" style={{ borderColor: 'var(--line-1)' }}>
                            <span
                                className="text-[10px] font-mono uppercase tracking-[0.12em] flex-1"
                                style={{ color: 'var(--fg-3)' }}
                            >
                                Copilot
                            </span>
                            <button
                                type="button"
                                onClick={() => setCopilotOpen((v) => !v)}
                                className="text-[10px] font-mono uppercase tracking-wider"
                                style={{ color: 'var(--fg-2)' }}
                            >
                                {copilotOpen ? '−' : '+'}
                            </button>
                        </div>
                        {copilotOpen && (
                            <>
                                <div
                                    className="flex-1 overflow-y-auto px-3 py-2 text-xs space-y-2"
                                    style={{ color: 'var(--fg-2)' }}
                                >
                                    <div className="px-2 py-1.5 rounded" style={{ background: 'var(--bg-2)' }}>
                                        <Pill tone="accent" dot>
                                            READY
                                        </Pill>
                                        <div className="mt-1 text-xs">
                                            Ask about{' '}
                                            <span style={{ color: 'var(--fg-0)' }}>{project.project_name}</span> —
                                            geology, holes, ore zones, or analogues.
                                        </div>
                                    </div>
                                    <div
                                        className="text-[10px] font-mono uppercase tracking-wider pt-2"
                                        style={{ color: 'var(--fg-3)' }}
                                    >
                                        Quick prompts
                                    </div>
                                    {copilotQuickPrompts(project.commodity).map((q) => (
                                        <Link
                                            key={q}
                                            href={`/projects/${project.slug}/chat?prompt=${encodeURIComponent(q)}`}
                                            className="block text-left text-[11px] px-2 py-1.5 rounded border hover:opacity-80"
                                            style={{
                                                borderColor: 'var(--line-1)',
                                                color: 'var(--fg-1)',
                                                background: 'var(--bg-2)',
                                            }}
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
                                        aria-label="Ask a question about this project"
                                        value={copilotPrompt}
                                        onChange={(e) => setCopilotPrompt(e.target.value)}
                                        placeholder="Ask a question…"
                                        className="text-xs px-2 py-1.5 rounded border"
                                        style={{
                                            borderColor: 'var(--line-2)',
                                            color: 'var(--fg-0)',
                                            background: 'var(--bg-2)',
                                        }}
                                    />
                                    <div className="flex gap-2">
                                        <button
                                            type="submit"
                                            disabled={!copilotPrompt.trim()}
                                            className="flex-1 text-[10px] font-mono uppercase tracking-wider px-2 py-1.5 rounded border disabled:opacity-40"
                                            style={{
                                                color: 'var(--accent)',
                                                borderColor: 'var(--accent-dim)',
                                                background: 'var(--accent-bg)',
                                            }}
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
        </>
    );
}

/**
 * LEFT / RIGHT hole selector for COMPARE mode.
 *
 * Reads the `collars` prop the page already loaded rather than issuing the
 * separate 200-row `pickable` query the deleted HoleCompareController ran —
 * one fewer round trip, and the two lists can no longer disagree.
 */
function ComparePicker({
    label,
    value,
    collars,
    onChange,
}: {
    label: string;
    value: string;
    collars: Collar[];
    onChange: (v: string) => void;
}) {
    return (
        <label className="flex items-center gap-2">
            <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                {label}
            </span>
            <select
                value={value}
                onChange={(e) => onChange(e.target.value)}
                className="px-2 py-1 text-xs font-mono rounded border"
                style={{ background: 'var(--bg-2)', color: 'var(--fg-0)', borderColor: 'var(--line-2)' }}
            >
                <option value="">— pick hole —</option>
                {collars.map((c) => {
                    const id = c.hole_id_canonical ?? c.hole_id;
                    return (
                        <option key={c.collar_id} value={id}>
                            {id}
                        </option>
                    );
                })}
            </select>
        </label>
    );
}

function LogsHolePicker({
    projectSlug,
    activeHoleId,
    holes,
}: {
    projectSlug: string;
    activeHoleId: string | null;
    holes: string[];
}) {
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
                only: [...LOG_PROPS],
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
                aria-label="Jump to hole"
                value={activeHoleId ?? ''}
                onChange={(e) => jumpTo(e.target.value)}
                className="text-[11px] font-mono px-2 py-1 rounded border"
                style={{ borderColor: 'var(--line-2)', color: 'var(--fg-1)', background: 'var(--bg-2)' }}
            >
                {holes.map((h) => (
                    <option key={h} value={h}>
                        {h}
                    </option>
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
