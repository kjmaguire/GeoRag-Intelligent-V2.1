import { Head, Link, router } from '@inertiajs/react';
import { useMemo } from 'react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, Stat, EmptyState } from '@/Components/Foundry/primitives';
import { DataQualityBadge } from '@/Components/Foundry/DataQualityBadge';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';
import DataQualityFlagsBadge from '@/Components/DataQualityFlagsBadge';

/**
 * Foundry DrillholeDetail — §5.12 anchored-scroll per-hole page.
 *
 * Single page, sticky collar header, four anchored sections (Strip Log /
 * Assays / Structures / Cross Section). Designed for print-to-PDF as a
 * single report; tabs were rejected in the kickoff decision.
 *
 * All visuals read pre-computed gold rows. When a section is empty the
 * page renders an EmptyState explaining which gold asset hasn't run.
 */

type GeorefMethod = 'declared' | 'detected' | 'assumed' | 'manual' | 'survey';

interface Collar {
    collar_id: string;
    hole_id: string;
    project_id: string;
    workspace_id?: string;
    elevation_m?: number | null;
    total_depth_m?: number | null;
    azimuth_deg?: number | null;
    dip_deg?: number | null;
    // CC-01 Item 2 — spatial uncertainty + CRS provenance.
    spatial_uncertainty_m?: number | null;
    crs_confidence?: number | null;
    georef_method?: GeorefMethod | null;
}

interface Interval {
    depth_from: number;
    depth_to: number;
    interval_kind: string;
    lithology_code?: string | null;
    lithology_label?: string | null;
    color_hint?: string | null;
    assay_payload?: Record<string, unknown>;
}

interface AssayRow {
    sample_id?: string;
    from_depth?: number;
    to_depth?: number;
    element?: string;
    value?: number;
    value_ppm?: number;
}

interface StructureRow {
    depth: number;
    structure_type: string;
    stereonet_x: number | null;
    stereonet_y: number | null;
    strike_deg?: number | null;
    dip_deg?: number | null;
}

interface CrossSectionRow {
    panel_id: string;
    section_name: string;
    hole_count: number;
}

interface QaIssue {
    severity: 'critical' | 'warning';
    field: 'depth_range' | 'lithology' | 'structure' | 'trace_geometry' | 'other';
    message: string;
}

interface QaPayload {
    qa?: {
        visualization_ready: boolean;
        issues: QaIssue[];
        supported_visualizations: string[];
        summary?: string;
    };
    inventory?: Record<string, number | boolean>;
}

interface LithologyQualityCounters {
    exact: number;
    fuzzy: number;
    unmapped: number;
    total: number;
}

interface Props {
    project: { project_id: string; project_name: string; slug: string };
    collar: Collar;
    intervals: Interval[];
    assays: AssayRow[];
    structures: StructureRow[];
    cross_sections: CrossSectionRow[];
    qa: QaPayload | null;
    lithology_quality: LithologyQualityCounters | null;
    // Plan §6a — silver.data_quality_flags rollup for this collar.
    // Null when the DB query failed; empty counts (open_total=0) when
    // the collar passes every rule (badge hides itself).
    data_quality_flags?: import('@/Components/DataQualityFlagsBadge').DataQualityFlagsBadgeData | null;
}

const QA_FIELD_TO_SECTION: Record<QaIssue['field'], string | null> = {
    depth_range:    'strip-log',
    lithology:      'strip-log',
    structure:      'structures',
    trace_geometry: 'cross-section',
    other:          null,
};

function issuesForSection(qa: QaPayload | null, sectionId: string): QaIssue[] {
    if (!qa?.qa?.issues) return [];
    return qa.qa.issues.filter(i => QA_FIELD_TO_SECTION[i.field] === sectionId);
}

function worstSeverity(issues: QaIssue[]): 'critical' | 'warning' | null {
    if (issues.some(i => i.severity === 'critical')) return 'critical';
    if (issues.some(i => i.severity === 'warning')) return 'warning';
    return null;
}

const SECTIONS = [
    { id: 'strip-log',   label: 'Strip Log' },
    { id: 'assays',      label: 'Assays' },
    { id: 'structures',  label: 'Structures' },
    { id: 'cross-section', label: 'Cross Section' },
];

export default function DrillholeDetail({ project, collar, intervals, assays, structures, cross_sections, qa, lithology_quality, data_quality_flags }: Props) {
    // Reliability spec Phase 2b — drill-hole-level data depends on
    // silver.collars + silver.intervals + silver.assays. Refetch the
    // relevant Inertia props if this project saw collars/assays move.
    useWorkspaceDataUpdated(project.project_id, (evt) => {
        const t = evt.affected_types;
        if (t.includes('collars') || t.includes('assays')) {
            router.reload({
                only: ['collar', 'intervals', 'assays', 'structures', 'cross_sections', 'qa', 'lithology_quality', 'data_quality_flags'],
            });
        }
    });

    const maxDepth = collar.total_depth_m ?? Math.max(...intervals.map(i => i.depth_to), 100);
    const qaIssues = qa?.qa?.issues ?? [];
    const qaReady = qa?.qa?.visualization_ready ?? null;
    const stripLogIssues = issuesForSection(qa, 'strip-log');
    const structuresIssues = issuesForSection(qa, 'structures');
    const crossSectionIssues = issuesForSection(qa, 'cross-section');

    return (
        <AppLayout>
            <Head title={`${collar.hole_id} · ${project.project_name}`} />

            <div className="flex-1 overflow-y-auto" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <div className="sticky top-0 z-10" style={{ background: 'var(--bg-0)', borderBottom: '1px solid var(--line-1)' }}>
                    <PageHeader
                        eyebrow={`HOLE · ${project.project_name.toUpperCase()}`}
                        title={collar.hole_id}
                        sub={`${(collar.total_depth_m ?? 0).toFixed(1)} m total depth · ${collar.azimuth_deg ?? '—'}° az · ${collar.dip_deg ?? '—'}° dip`}
                        actions={
                            <div className="flex items-center gap-2">
                                {/* Plan §6a — data-quality flags badge.
                                    Hidden when this collar has 0 open flags. */}
                                <DataQualityFlagsBadge data={data_quality_flags} />
                                <Link
                                    href={`/projects/${project.slug}/lakehouse`}
                                    className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                    style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                                >
                                    ← Lakehouse
                                </Link>
                            </div>
                        }
                    />

                    <nav className="flex gap-1 px-8 py-2" aria-label="Section navigation">
                        {SECTIONS.map(s => (
                            <a
                                key={s.id}
                                href={`#${s.id}`}
                                className="text-xs font-mono uppercase tracking-wider px-3 py-1 rounded border"
                                style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                            >
                                {s.label}
                            </a>
                        ))}
                    </nav>

                    <section className="grid grid-cols-4 gap-px px-8 py-3" style={{ background: 'var(--line-1)' }}>
                        <Stat label="INTERVALS" value={String(intervals.length)} />
                        <Stat label="ASSAYS" value={String(assays.length)} />
                        <Stat label="STRUCTURES" value={String(structures.length)} />
                        <Stat label="CROSS-SECTIONS" value={String(cross_sections.length)} />
                    </section>

                    {(qa?.qa || lithology_quality) && (
                        <div
                            className="flex items-center gap-3 px-8 py-2 border-t text-xs"
                            style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}
                        >
                            {qa?.qa && (
                                <>
                                    <Pill tone={qaReady ? 'accent' : (worstSeverity(qaIssues) === 'critical' ? 'danger' : 'warn')} dot>
                                        {qaReady ? 'Visual QA: ready' : 'Visual QA: ' + (worstSeverity(qaIssues) ?? 'unknown')}
                                    </Pill>
                                    <span style={{ color: 'var(--fg-2)' }}>
                                        {qa.qa.summary ?? `${qaIssues.length} issue${qaIssues.length === 1 ? '' : 's'}`}
                                    </span>
                                </>
                            )}
                            {lithology_quality && (
                                <DataQualityBadge
                                    counters={lithology_quality}
                                    href={`/projects/${project.slug}/ingest-quality?hole=${encodeURIComponent(collar.hole_id)}`}
                                />
                            )}
                            <SpatialConfidenceBadge collar={collar} />
                            {qa?.qa && (
                                <span className="ml-auto font-mono" style={{ color: 'var(--fg-3)' }}>
                                    supports: {qa.qa.supported_visualizations.join(', ') || '∅'}
                                </span>
                            )}
                        </div>
                    )}
                    {!(qa?.qa || lithology_quality) && (collar.crs_confidence != null || !!collar.georef_method) && (
                        <div
                            className="flex items-center gap-3 px-8 py-2 border-t text-xs"
                            style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}
                        >
                            <SpatialConfidenceBadge collar={collar} />
                        </div>
                    )}
                </div>

                <section id="strip-log" className="px-8 py-6">
                    <Card
                        eyebrow="STRIP LOG"
                        title={`Lithology + assay overlay · ${intervals.length} intervals`}
                        actions={<SectionQaPill issues={stripLogIssues} />}
                    >
                        {intervals.length === 0 ? (
                            <EmptyState
                                title="No strip-log intervals yet."
                                detail="Materialise gold.drillhole_intervals_visual via Dagster to populate this section."
                            />
                        ) : (
                            <StripLog intervals={intervals} maxDepth={maxDepth} />
                        )}
                    </Card>
                </section>

                <section id="assays" className="px-8 py-6">
                    <Card eyebrow="ASSAYS" title={`Top ${assays.length} by value`} padded={false}>
                        {assays.length === 0 ? (
                            <div className="px-4 py-6">
                                <EmptyState
                                    title="No assays for this hole."
                                    detail="Either silver.assays_v2 has no rows for this collar, or the migration that adds value_ppm hasn't run."
                                />
                            </div>
                        ) : (
                            <AssayTable rows={assays} />
                        )}
                    </Card>
                </section>

                <section id="structures" className="px-8 py-6">
                    <Card
                        eyebrow="STRUCTURES"
                        title={`Stereonet · ${structures.length} measurements`}
                        actions={<SectionQaPill issues={structuresIssues} />}
                    >
                        {structures.length === 0 ? (
                            <EmptyState
                                title="No structure measurements yet."
                                detail="Materialise gold.structure_measurements_visual via Dagster (equal-area projection)."
                            />
                        ) : (
                            <Stereonet points={structures} />
                        )}
                    </Card>
                </section>

                <section id="cross-section" className="px-8 py-6 pb-12">
                    <Card
                        eyebrow="CROSS SECTION"
                        title={`${cross_sections.length} panels intersecting this hole`}
                        padded={false}
                        actions={<SectionQaPill issues={crossSectionIssues} />}
                    >
                        {cross_sections.length === 0 ? (
                            <div className="px-4 py-6">
                                <EmptyState
                                    title="No cross-section panels yet."
                                    detail="Materialise gold.cross_section_panels with this hole inside the tolerance corridor."
                                />
                            </div>
                        ) : (
                            <div>
                                {cross_sections.map(p => (
                                    <div
                                        key={p.panel_id}
                                        className="flex justify-between items-center px-4 py-3 border-b"
                                        style={{ borderColor: 'var(--line-1)' }}
                                    >
                                        <div style={{ color: 'var(--fg-0)' }}>{p.section_name}</div>
                                        <div className="flex gap-3 items-center">
                                            <Pill tone="neutral" dot>{p.hole_count} holes</Pill>
                                        </div>
                                    </div>
                                ))}
                            </div>
                        )}
                    </Card>
                </section>
            </div>
        </AppLayout>
    );
}

/**
 * CC-01 Item 2 — Spatial confidence badge.
 *
 * Surfaces `silver.collars.crs_confidence` (0-1) as a percentage and pairs it
 * with the `georef_method` vocabulary. Hidden entirely when neither column
 * has a value — so the header bar stays clean for legacy rows that haven't
 * been touched by the backfill job yet.
 *
 * The native `title` attribute carries the vocabulary tooltip; this keeps
 * the markup framework-free (no shadcn Tooltip dependency on what is a tiny
 * informational chip).
 */
const GEOREF_METHOD_VOCAB: Record<GeorefMethod, { tone: 'accent' | 'info' | 'warn' | 'danger' | 'neutral'; help: string }> = {
    declared: { tone: 'accent', help: 'declared — CRS stated explicitly in source metadata' },
    detected: { tone: 'info',   help: 'detected — CRS inferred by the spatial pipeline from coordinate ranges' },
    assumed:  { tone: 'warn',   help: 'assumed — fallback projection (e.g. UTM zone derived from project bbox)' },
    manual:   { tone: 'accent', help: 'manual — geologist set the CRS / location in the UI' },
    survey:   { tone: 'accent', help: 'survey — exact survey instrument datum (highest provenance)' },
};

function SpatialConfidenceBadge({ collar }: { collar: Collar }) {
    const cc = collar.crs_confidence;
    const gm = collar.georef_method ?? null;
    if ((cc === null || cc === undefined) && !gm) return null;

    const pct = cc !== null && cc !== undefined ? Math.round(cc * 100) : null;
    const vocab = gm ? GEOREF_METHOD_VOCAB[gm] : null;
    const tone = vocab?.tone ?? 'neutral';

    const tooltip = [
        pct !== null ? `Spatial confidence: ${pct}%` : 'Spatial confidence: unknown',
        gm ? `Georef method: ${vocab?.help ?? gm}` : null,
        '',
        'Vocabulary:',
        ...(Object.entries(GEOREF_METHOD_VOCAB) as Array<[GeorefMethod, { help: string }]>).map(([, v]) => `  • ${v.help}`),
    ].filter(Boolean).join('\n');

    return (
        <span title={tooltip} className="inline-flex items-center gap-1.5">
            <Pill tone={tone} dot>
                Spatial {pct !== null ? `${pct}%` : '—'}
            </Pill>
            {gm && (
                <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                    {gm}
                </span>
            )}
        </span>
    );
}

function SectionQaPill({ issues }: { issues: QaIssue[] }) {
    if (issues.length === 0) return null;
    const sev = worstSeverity(issues);
    const tone = sev === 'critical' ? 'danger' : 'warn';
    return (
        <Pill tone={tone} dot>
            {issues.length} {sev}
        </Pill>
    );
}

function StripLog({ intervals, maxDepth }: { intervals: Interval[]; maxDepth: number }) {
    const totalDepth = maxDepth || 1;
    return (
        <div className="relative" style={{ height: 400 }}>
            <svg viewBox="0 0 200 1000" preserveAspectRatio="none" className="w-full h-full">
                {intervals.map((iv, i) => {
                    const yStart = (iv.depth_from / totalDepth) * 1000;
                    const height = ((iv.depth_to - iv.depth_from) / totalDepth) * 1000;
                    return (
                        <g key={i}>
                            <rect
                                x={0}
                                y={yStart}
                                width={120}
                                height={height}
                                fill={iv.color_hint ?? '#666'}
                                stroke="rgba(0,0,0,0.15)"
                                strokeWidth={0.5}
                            />
                            <text
                                x={130}
                                y={yStart + height / 2 + 4}
                                fontSize={10}
                                fontFamily="ui-monospace, monospace"
                                fill="var(--fg-2)"
                            >
                                {iv.lithology_code ?? iv.interval_kind}
                            </text>
                        </g>
                    );
                })}
            </svg>
        </div>
    );
}

function AssayTable({ rows }: { rows: AssayRow[] }) {
    return (
        <>
            <div className="grid grid-cols-[1fr_100px_100px_80px_100px] text-[10px] font-mono uppercase tracking-wider px-4 py-2 border-b" style={{ color: 'var(--fg-3)', borderColor: 'var(--line-1)' }}>
                <div>Sample</div>
                <div>From</div>
                <div>To</div>
                <div>Element</div>
                <div>Value</div>
            </div>
            {rows.map((r, i) => (
                <div
                    key={i}
                    className="grid grid-cols-[1fr_100px_100px_80px_100px] text-xs px-4 py-2 border-b"
                    style={{ borderColor: 'var(--line-1)' }}
                >
                    <div className="truncate" style={{ color: 'var(--fg-0)' }}>{r.sample_id ?? '—'}</div>
                    <div className="font-mono" style={{ color: 'var(--fg-2)' }}>{r.from_depth !== undefined ? Number(r.from_depth).toFixed(2) : '—'}</div>
                    <div className="font-mono" style={{ color: 'var(--fg-2)' }}>{r.to_depth !== undefined ? Number(r.to_depth).toFixed(2) : '—'}</div>
                    <div className="font-mono" style={{ color: 'var(--fg-2)' }}>{r.element ?? '—'}</div>
                    <div className="font-mono" style={{ color: 'var(--fg-0)' }}>
                        {r.value_ppm !== undefined && r.value_ppm !== null
                            ? `${r.value_ppm} ppm`
                            : (r.value !== undefined && r.value !== null ? String(r.value) : '—')}
                    </div>
                </div>
            ))}
        </>
    );
}

function Stereonet({ points }: { points: StructureRow[] }) {
    const valid = useMemo(
        () => points.filter(p => p.stereonet_x !== null && p.stereonet_y !== null),
        [points],
    );
    return (
        <div className="flex justify-center">
            <svg viewBox="-1.6 -1.6 3.2 3.2" className="w-64 h-64">
                <circle cx={0} cy={0} r={Math.SQRT2} fill="none" stroke="var(--line-2)" strokeWidth={0.02} />
                <line x1={0} y1={-Math.SQRT2} x2={0} y2={Math.SQRT2} stroke="var(--line-1)" strokeWidth={0.01} />
                <line x1={-Math.SQRT2} y1={0} x2={Math.SQRT2} y2={0} stroke="var(--line-1)" strokeWidth={0.01} />
                {valid.map((p, i) => (
                    <circle
                        key={i}
                        cx={p.stereonet_x ?? 0}
                        cy={-(p.stereonet_y ?? 0)}
                        r={0.025}
                        fill={p.structure_type === 'fault' ? '#dc2626' : p.structure_type === 'bedding' ? '#2563eb' : 'var(--fg-1)'}
                    />
                ))}
            </svg>
        </div>
    );
}
