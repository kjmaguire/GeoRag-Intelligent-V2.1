// §6b P1+P5 (2026-05-29) — @ts-nocheck removed. VizPayloadMeta is the
// typed wire-shape for `plotly_layout.meta`; the per-card branches below
// narrow on `chart_type` and read only the meta fields that variant
// populates. Child components own their internal row types and accept
// unknown[] from this dispatcher.
import { lazy, Suspense, useState } from 'react';
import type { MapPayload, VizPayload, VizPayloadMeta } from '@/types';
import type { CollarPoint, IntervalPoint, StructurePoint } from './DrillTrace3D';
import type { CoverageRow, IngestGap } from './CoverageTableCard';
import type { GraphNode, GraphEdge } from './KnowledgeGraph';
import type { StereonetMeta } from './StereonetCard';
import type { TimelineSwimlane } from './TimelineCard';

const MapView = lazy(() => import('./MapView'));
const StripLogViewer = lazy(() => import('./StripLogViewer'));
const GeoPlot = lazy(() => import('./GeoPlot'));
const KnowledgeGraph = lazy(() => import('./KnowledgeGraph'));
const DrillTrace3D = lazy(() => import('./DrillTrace3D'));
const TimelineCard = lazy(() => import('./TimelineCard'));
const CoverageTableCard = lazy(() => import('./CoverageTableCard'));
const StereonetCard = lazy(() => import('./StereonetCard'));

function LoadingPanel({ label }: { label: string }) {
    return (
        <div className="flex items-center justify-center h-full bg-gray-950/60 text-xs text-gray-500">
            <div className="w-4 h-4 rounded-full border-2 border-gray-700 border-t-amber-400 animate-spin mr-2" />
            {label}
        </div>
    );
}

interface VizCardProps {
    title: string;
    badge?: string | null;
    onClose: () => void;
    children: React.ReactNode;
    heightClass?: string;
}

function VizCard({ title, badge, onClose, children, heightClass = 'h-72' }: VizCardProps) {
    return (
        <div className="mt-3 border border-gray-700/80 rounded-xl overflow-hidden bg-gray-900/90 shadow-lg">
            <div className="flex items-center justify-between px-3 py-2 border-b border-gray-800 bg-gray-900">
                <div className="flex items-center gap-2 min-w-0">
                    {badge && (
                        <span className="text-[10px] uppercase tracking-wider font-semibold text-amber-400 bg-amber-950/60 border border-amber-800/60 px-1.5 py-0.5 rounded">
                            {badge}
                        </span>
                    )}
                    <span className="text-xs text-gray-300 font-medium truncate">{title}</span>
                </div>
                <button
                    type="button"
                    onClick={onClose}
                    className="text-gray-500 hover:text-gray-200 focus:outline-none focus:text-gray-200 p-1 rounded"
                    aria-label="Hide visualization"
                >
                    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" className="w-3.5 h-3.5" aria-hidden="true">
                        <path fillRule="evenodd" d="M5.47 5.47a.75.75 0 0 1 1.06 0L12 10.94l5.47-5.47a.75.75 0 1 1 1.06 1.06L13.06 12l5.47 5.47a.75.75 0 1 1-1.06 1.06L12 13.06l-5.47 5.47a.75.75 0 0 1-1.06-1.06L10.94 12 5.47 6.53a.75.75 0 0 1 0-1.06Z" clipRule="evenodd" />
                    </svg>
                </button>
            </div>
            <div className={`relative ${heightClass} bg-gray-950`}>{children}</div>
        </div>
    );
}

interface InlineVizProps {
    mapPayload?: { geojson?: MapPayload; bbox?: [number, number, number, number]; label?: string } | null;
    vizPayload?: VizPayload | null;
    projectId?: string | null;
}

export default function InlineViz({ mapPayload, vizPayload, projectId }: InlineVizProps) {
    const [mapHidden, setMapHidden] = useState<boolean>(false);
    const [vizHidden, setVizHidden] = useState<boolean>(false);

    const hasMap = !!mapPayload && !mapHidden;

    const vizType = vizPayload?.chart_type;
    // The typed VizPayloadMeta narrows the per-card field reads without
    // casts. Empty object fallback so destructuring/optional-chaining
    // never crashes on null/undefined meta.
    const meta: VizPayloadMeta = vizPayload?.plotly_layout?.meta ?? {};

    const hasStripLog = !vizHidden && vizType === 'downhole_strip' && !!meta.hole_id;
    const plotlyData = vizPayload?.plotly_data;
    const hasPlotly = !vizHidden
        && (vizType === 'assay_histogram' || vizType === 'cross_section')
        && (plotlyData?.length ?? 0) > 0;
    const graphNodes = meta.nodes ?? [];
    const hasGraphViz = !vizHidden && vizType === 'graph_viz' && graphNodes.length > 0;
    const traceCollars = meta.collars ?? [];
    const traceIntervals = meta.intervals ?? [];
    const traceStructures = meta.structures ?? [];
    const has3DTrace = !vizHidden && vizType === 'drill_trace_3d' && traceCollars.length > 0;
    const swimlanes = meta.swimlanes ?? [];
    const hasTimeline = !vizHidden && vizType === 'technique_timeline' && swimlanes.length > 0;
    const coverageRows = meta.rows ?? [];
    const hasCoverage = !vizHidden && vizType === 'coverage_table' && coverageRows.length > 0;
    const stereonetImage = meta.image_base64;
    const hasStereonet = !vizHidden
        && vizType === 'stereonet'
        && typeof stereonetImage === 'string'
        && stereonetImage.length > 0;
    const hasViz = hasStripLog || hasPlotly || hasGraphViz || has3DTrace || hasTimeline || hasCoverage || hasStereonet;

    if (!hasMap && !hasViz) return null;

    const featureCount = mapPayload?.geojson?.features?.length ?? 0;
    const mapTitle = mapPayload?.label || `Drill collars (${featureCount})`;

    return (
        <div className="w-full">
            {hasMap && (
                <VizCard title={mapTitle} badge="Map" onClose={() => setMapHidden(true)} heightClass="h-72">
                    <Suspense fallback={<LoadingPanel label="Loading map…" />}>
                        {/* MapView's `inlineGeoJson` prop uses a permissive
                            `coordinates: number[]` shape; @/types' MapPayload
                            uses strict GeoJSON.Position. The cast at this
                            boundary is the documented integration point
                            until we harmonise the two type definitions in
                            a follow-up. */}
                        <MapView
                            inlineGeoJson={mapPayload!.geojson as unknown as Parameters<typeof MapView>[0]['inlineGeoJson']}
                            inlineBbox={mapPayload!.bbox}
                            compact
                        />
                    </Suspense>
                </VizCard>
            )}

            {hasStripLog && (
                <VizCard title={vizPayload?.title || `Strip log — ${meta.hole_id}`} badge="Strip Log" onClose={() => setVizHidden(true)} heightClass="h-96">
                    <Suspense fallback={<LoadingPanel label="Loading strip log…" />}>
                        {/* hole_id is guaranteed truthy by hasStripLog above. */}
                        <StripLogViewer holeId={meta.hole_id!} projectId={projectId ?? undefined} />
                    </Suspense>
                </VizCard>
            )}

            {hasPlotly && (
                <VizCard title={vizPayload?.title || 'Assay Data'} badge="Chart" onClose={() => setVizHidden(true)} heightClass="h-80">
                    <Suspense fallback={<LoadingPanel label="Loading chart…" />}>
                        <GeoPlot data={plotlyData!} layout={vizPayload!.plotly_layout!} />
                    </Suspense>
                </VizCard>
            )}

            {hasGraphViz && (
                <VizCard title={vizPayload?.title || 'Knowledge Graph'} badge="Graph" onClose={() => setVizHidden(true)} heightClass="h-96">
                    <Suspense fallback={<LoadingPanel label="Loading graph…" />}>
                        {/* Wire shape is unknown[] from FastAPI; child component
                            owns the runtime narrowing. Cast at the boundary
                            documents the contract. */}
                        <KnowledgeGraph
                            graphNodes={graphNodes as GraphNode[]}
                            graphEdges={(meta.edges ?? []) as GraphEdge[]}
                        />
                    </Suspense>
                </VizCard>
            )}

            {has3DTrace && (
                <VizCard title={vizPayload?.title || '3D Drill Traces'} badge="3D" onClose={() => setVizHidden(true)} heightClass="h-96">
                    <Suspense fallback={<LoadingPanel label="Loading 3D view…" />}>
                        <DrillTrace3D
                            collars={traceCollars as CollarPoint[]}
                            intervals={traceIntervals as IntervalPoint[]}
                            structures={traceStructures as StructurePoint[]}
                        />
                    </Suspense>
                </VizCard>
            )}

            {hasTimeline && (
                <VizCard title={vizPayload?.title || 'Technique Timeline'} badge="Timeline" onClose={() => setVizHidden(true)} heightClass="h-80">
                    <Suspense fallback={<LoadingPanel label="Loading timeline…" />}>
                        <TimelineCard swimlanes={swimlanes as TimelineSwimlane[]} title={vizPayload?.title} />
                    </Suspense>
                </VizCard>
            )}

            {hasCoverage && (
                <VizCard title={vizPayload?.title || 'Coverage'} badge="Coverage" onClose={() => setVizHidden(true)} heightClass="h-96">
                    <Suspense fallback={<LoadingPanel label="Loading coverage…" />}>
                        <CoverageTableCard
                            rows={coverageRows as CoverageRow[]}
                            ingestGap={(meta.ingest_gap ?? null) as IngestGap | null}
                            title={vizPayload?.title}
                        />
                    </Suspense>
                </VizCard>
            )}

            {hasStereonet && (
                <VizCard title={vizPayload?.title || 'Stereonet'} badge="Stereonet" onClose={() => setVizHidden(true)} heightClass="h-96">
                    <Suspense fallback={<LoadingPanel label="Loading stereonet…" />}>
                        <StereonetCard meta={meta as StereonetMeta} />
                    </Suspense>
                </VizCard>
            )}
        </div>
    );
}
