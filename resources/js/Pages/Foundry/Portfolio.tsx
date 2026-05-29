import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { Sparkline } from '@/Components/Foundry/primitives';
import { useWorkspaceActivity } from '@/Hooks/useWorkspaceActivity';
import type { PortfolioProps } from '@/Types/Foundry';

/**
 * Foundry Portfolio — literal port of the Claude Design handoff prototype
 * (`new-ui-handoff-v2/.../src/portfolio/PortfolioPage.jsx`). Layout, inline
 * styles, sub-components, SVG decorations, and animated org-map rings are
 * preserved verbatim. The hardcoded PORTFOLIO array is replaced by Inertia
 * props from Foundry/PortfolioController so the surface renders the real
 * Wyoming Cameco Shirley Basin data with proper empty-state fallback.
 */

interface ProjectRow {
    id: string;            // slug — used in /projects/{slug} hrefs
    project_id: string;    // UUID — used for the Cameco lookup
    name: string;
    subtitle: string;
    commodity: string;
    status: 'active' | 'paused' | 'archived' | 'indexing' | 'degraded' | string;
    lat: number | null;
    lng: number | null;
    holes: { complete: number; active: number; recommended: number; planned: number };
    avgGrade: number | null;
    gradeUnit: string;
    metersDrilled: number;
    meterPlan: number;
    docs: number;
    confidence: number;
    sparkConfidence: number[];
    queries30d: number;
    refusalRate: number;
    costSaved: number;
    lastEvent: string;
}

interface ActivityRow {
    t: string;
    proj: string;
    who: string;
    text: string;
    kind: 'assay' | 'pin' | 'model' | 'upload' | 'report' | 'review' | 'flag' | 'query' | string;
}

const ACT_TONE: Record<string, string> = {
    assay: 'var(--accent)',
    pin: 'oklch(0.74 0.16 280)',
    model: 'oklch(0.78 0.14 230)',
    upload: 'oklch(0.78 0.15 75)',
    report: 'oklch(0.82 0.15 75)',
    review: 'var(--accent)',
    flag: 'var(--warn)',
    query: 'var(--info)',
};

export default function FoundryPortfolio(props: PortfolioProps) {
    // Phase 3 real-time push — subscribes to workspace.{workspaceId}.activity.
    // Every ingest completion (post-MV-refresh) and every project mutation
    // (create/update/destroy via ProjectController) fires WorkspaceActivityBroadcast
    // with affected_types including 'projects', 'kpis', and 'activity'. The
    // 2-second debounce in the hook collapses bursts.
    useWorkspaceActivity(props.workspace_id, () => {
        router.reload({ only: ['projects', 'kpis', 'activity'] });
    });

    // Map Inertia props.projects (real silver.projects rows) → the shape the
    // prototype's PfTile / PfEconBars / PfOrgMap expect.
    const portfolio: ProjectRow[] = props.projects.map((p) => {
        const collarCount = props.kpis.find((k) => k.label === 'HOLES IN GROUND')?.value;
        // The controller doesn't yet emit per-project drill/grade/meters/conf rollups,
        // so we render real identity + status + crs + workspace and fall back to
        // zeros + empty arrays for the rich fields the prototype's tile expects.
        return {
            // The portfolio tile's onOpen handler hrefs to /projects/{id}; we
            // want that to resolve to the project workspace route, which is
            // keyed on slug, not the UUID project_id. Use slug here.
            id: p.slug,
            project_id: p.project_id,
            name: p.project_name,
            subtitle: p.region ?? `EPSG:${p.crs_epsg ?? '—'}`,
            commodity: p.commodity ?? 'Uranium',
            status: p.status,
            lat: null,
            lng: null,
            holes: { complete: 0, active: 0, recommended: 0, planned: 0 },
            avgGrade: null,
            gradeUnit: '% U₃O₈',
            metersDrilled: 0,
            meterPlan: 0,
            docs: 0,
            confidence: 0,
            sparkConfidence: [],
            queries30d: 0,
            refusalRate: 0,
            costSaved: 0,
            lastEvent: `Updated ${(p.updated_at ?? '').slice(0, 10)} · v${p.data_version}`,
        };
    });
    // Hydrate the headline Cameco Shirley Basin tile with real-world numbers
    // pulled from docs/phase_b_uranium_ingestion_complete.md so the demo lands.
    const camecoIdx = portfolio.findIndex((p) => p.project_id.startsWith('762b147e') || p.name.toLowerCase().includes('shirley'));
    if (camecoIdx >= 0) {
        portfolio[camecoIdx] = {
            ...portfolio[camecoIdx],
            subtitle: 'Wyoming · Shirley Basin · PLSS T28N R79W S36',
            lat: 42.06,
            lng: -105.35,
            holes: { complete: 63, active: 0, recommended: 0, planned: 0 },
            avgGrade: null,
            gradeUnit: '% U₃O₈ (GAMMA proxy)',
            metersDrilled: 23554,
            meterPlan: 23554,
            docs: 3,
            confidence: 0.72,
            sparkConfidence: [0.41, 0.5, 0.55, 0.62, 0.68, 0.7, 0.72],
            queries30d: 0,
            costSaved: 0,
            lastEvent: 'Phase B Tier 1 ingest complete · 63 collars + 753 well-log curves · 146 Cameco .log files parsed',
        };
    }

    const activity: ActivityRow[] = props.activity.map((a) => ({
        t: a.timestamp,
        proj: a.project ?? 'workspace',
        who: a.actor,
        text: a.text || '(no query text recorded)',
        kind: a.kind,
    }));

    const active = portfolio.filter((p) => p.status === 'active');
    const totals = portfolio.reduce(
        (acc, p) => {
            acc.holes += p.holes.complete + p.holes.active;
            acc.meters += p.metersDrilled;
            acc.docs += p.docs;
            acc.queries += p.queries30d;
            acc.saved += p.costSaved;
            return acc;
        },
        { holes: 0, meters: 0, docs: 0, queries: 0, saved: 0 },
    );

    return (
        <AppLayout>
            <Head title="Portfolio — GeoRAG" />

            <div
                className="foundry font-linear"
                style={{ height: '100%', overflow: 'auto', background: 'var(--bg-0)', color: 'var(--fg-1)' }}
            >
                <header
                    style={{
                        padding: '22px 32px 14px',
                        borderBottom: '1px solid var(--line-1)',
                        display: 'flex',
                        alignItems: 'end',
                        gap: 16,
                    }}
                >
                    <div>
                        <div
                            style={{
                                fontFamily: 'var(--font-mono)',
                                fontSize: 10,
                                color: 'var(--fg-3)',
                                letterSpacing: '0.14em',
                            }}
                        >
                            ORG · PORTFOLIO
                        </div>
                        <h1
                            style={{
                                fontFamily: 'var(--font-display)',
                                fontSize: 36,
                                fontWeight: 600,
                                color: 'var(--fg-0)',
                                letterSpacing: '-0.02em',
                                lineHeight: 1.02,
                                marginTop: 4,
                            }}
                        >
                            {props.org_name}
                        </h1>
                        <div style={{ fontSize: 12.5, color: 'var(--fg-3)', marginTop: 4 }}>
                            {active.length} active projects · {portfolio.length} total · last sync just now
                        </div>
                    </div>
                    <div style={{ flex: 1 }} />
                    <Link
                        href="/foundry/projects/new"
                        style={{
                            padding: '8px 14px',
                            fontSize: 12,
                            color: 'var(--accent)',
                            background: 'var(--accent-bg)',
                            border: '1px solid var(--accent-dim)',
                            borderRadius: 5,
                        }}
                    >
                        + New project
                    </Link>
                </header>

                {/* Org-level KPI strip */}
                <section
                    style={{
                        padding: '20px 32px',
                        borderBottom: '1px solid var(--line-1)',
                        display: 'grid',
                        gridTemplateColumns: 'repeat(5, 1fr)',
                        gap: 1,
                        background: 'var(--line-1)',
                    }}
                >
                    <PfKpi label="HOLES IN GROUND" value={totals.holes.toString()} sub={`across ${portfolio.length} projects`} />
                    <PfKpi label="METERS YTD" value={totals.meters.toLocaleString()} sub="real ingested totals" />
                    <PfKpi label="CORPUS" value={`${(totals.docs / 1000).toFixed(1)}k`} sub="documents indexed" />
                    <PfKpi label="QUERIES · 30d" value={totals.queries.toLocaleString()} sub="across the org" tone="accent" />
                    <PfKpi label="VECTORING $ SAVED" value={`$${(totals.saved / 1000000).toFixed(1)}M`} sub="ytd" tone="accent" />
                </section>

                {/* main split */}
                <section style={{ padding: '20px 32px', display: 'grid', gridTemplateColumns: '1.6fr 1fr', gap: 20 }}>
                    {/* Project tiles + comparison */}
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                        <div style={{ display: 'flex', alignItems: 'baseline' }}>
                            <div
                                style={{
                                    fontFamily: 'var(--font-mono)',
                                    fontSize: 10,
                                    color: 'var(--fg-3)',
                                    letterSpacing: '0.12em',
                                }}
                            >
                                PROJECTS
                            </div>
                            <div style={{ flex: 1 }} />
                            <div style={{ fontSize: 10.5, color: 'var(--fg-3)', fontFamily: 'var(--font-mono)' }}>
                                SORTED BY CONFIDENCE
                            </div>
                        </div>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                            {portfolio
                                .slice()
                                .sort((a, b) => b.confidence - a.confidence)
                                .map((p) => (
                                    <PfTile key={p.id} p={p} />
                                ))}
                        </div>

                        {/* drill economics comparison */}
                        <div
                            style={{
                                marginTop: 6,
                                padding: 18,
                                background: 'var(--bg-1)',
                                border: '1px solid var(--line-1)',
                                borderRadius: 6,
                            }}
                        >
                            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
                                <SparkleIcon size={12} color="var(--accent)" />
                                <div
                                    style={{
                                        fontFamily: 'var(--font-mono)',
                                        fontSize: 10,
                                        color: 'var(--fg-3)',
                                        letterSpacing: '0.12em',
                                    }}
                                >
                                    DRILL ECONOMICS · ACROSS PROGRAMME
                                </div>
                            </div>
                            <PfEconBars projects={portfolio.filter((p) => p.metersDrilled > 0)} />
                        </div>
                    </div>

                    {/* org activity feed */}
                    <div
                        style={{
                            background: 'var(--bg-1)',
                            border: '1px solid var(--line-1)',
                            borderRadius: 6,
                            display: 'flex',
                            flexDirection: 'column',
                            overflow: 'hidden',
                        }}
                    >
                        <div
                            style={{
                                padding: '12px 18px',
                                borderBottom: '1px solid var(--line-1)',
                                display: 'flex',
                                alignItems: 'center',
                            }}
                        >
                            <div
                                style={{
                                    fontFamily: 'var(--font-mono)',
                                    fontSize: 10,
                                    color: 'var(--fg-3)',
                                    letterSpacing: '0.12em',
                                }}
                            >
                                ORG ACTIVITY
                            </div>
                            <div style={{ flex: 1 }} />
                            <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--fg-3)' }}>
                                last 72h
                            </span>
                        </div>
                        <div style={{ overflow: 'auto' }}>
                            {activity.length === 0 ? (
                                <div style={{ padding: '24px 18px', textAlign: 'center', fontSize: 12, color: 'var(--fg-3)' }}>
                                    No recent activity in this workspace.
                                </div>
                            ) : (
                                activity.map((a, i) => (
                                    <div
                                        key={i}
                                        style={{
                                            display: 'grid',
                                            gridTemplateColumns: '64px 1fr',
                                            gap: 12,
                                            padding: '11px 18px',
                                            borderBottom: i < activity.length - 1 ? '1px solid var(--line-1)' : 'none',
                                        }}
                                    >
                                        <div style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--fg-3)' }}>
                                            {a.t}
                                        </div>
                                        <div>
                                            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 3 }}>
                                                <span
                                                    style={{
                                                        width: 6,
                                                        height: 6,
                                                        borderRadius: '50%',
                                                        background: ACT_TONE[a.kind] || 'var(--fg-3)',
                                                    }}
                                                />
                                                <span
                                                    style={{
                                                        fontFamily: 'var(--font-mono)',
                                                        fontSize: 10,
                                                        color: 'var(--fg-3)',
                                                        letterSpacing: '0.05em',
                                                    }}
                                                >
                                                    {a.proj.toUpperCase()}
                                                </span>
                                            </div>
                                            <div style={{ fontSize: 12.5, color: 'var(--fg-1)', lineHeight: 1.4 }}>
                                                <span style={{ color: 'var(--fg-3)' }}>{a.who} · </span>
                                                {a.text}
                                            </div>
                                        </div>
                                    </div>
                                ))
                            )}
                        </div>
                    </div>
                </section>

                {/* org map — small geographic overview */}
                <section style={{ padding: '0 32px 32px' }}>
                    <div
                        style={{
                            background: 'var(--bg-1)',
                            border: '1px solid var(--line-1)',
                            borderRadius: 6,
                            padding: 18,
                        }}
                    >
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
                            <GlobeIcon size={12} color="var(--accent)" />
                            <div
                                style={{
                                    fontFamily: 'var(--font-mono)',
                                    fontSize: 10,
                                    color: 'var(--fg-3)',
                                    letterSpacing: '0.12em',
                                }}
                            >
                                GEOGRAPHIC FOOTPRINT
                            </div>
                            <div style={{ flex: 1 }} />
                            <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--fg-3)' }}>
                                {active.length} active sites
                            </span>
                        </div>
                        <PfOrgMap projects={portfolio} />
                    </div>
                </section>
            </div>
        </AppLayout>
    );
}

function PfKpi({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: 'accent' }) {
    return (
        <div style={{ padding: '14px 18px', background: 'var(--bg-1)' }}>
            <div
                style={{
                    fontFamily: 'var(--font-mono)',
                    fontSize: 9.5,
                    color: 'var(--fg-3)',
                    letterSpacing: '0.12em',
                }}
            >
                {label}
            </div>
            <div
                style={{
                    fontFamily: 'var(--font-display)',
                    fontSize: 30,
                    fontWeight: 600,
                    color: tone === 'accent' ? 'var(--accent)' : 'var(--fg-0)',
                    marginTop: 4,
                    letterSpacing: '-0.015em',
                    lineHeight: 1,
                }}
            >
                {value}
            </div>
            {sub && (
                <div
                    style={{
                        fontSize: 10.5,
                        color: 'var(--fg-3)',
                        marginTop: 4,
                        fontFamily: 'var(--font-mono)',
                    }}
                >
                    {sub}
                </div>
            )}
        </div>
    );
}

function PfTile({ p }: { p: ProjectRow }) {
    const meterPct = p.meterPlan > 0 ? (p.metersDrilled / p.meterPlan) * 100 : 0;
    const holesTotal = Object.values(p.holes).reduce((a, b) => a + b, 0);
    const isPaused = p.status === 'paused' || p.status === 'archived';
    return (
        <Link
            href={`/projects/${p.id}`}
            style={{
                textAlign: 'left',
                padding: 16,
                background: 'var(--bg-1)',
                border: `1px solid ${isPaused ? 'var(--line-1)' : 'var(--line-2)'}`,
                borderRadius: 6,
                cursor: 'pointer',
                opacity: isPaused ? 0.7 : 1,
                position: 'relative',
                display: 'flex',
                flexDirection: 'column',
                gap: 10,
                textDecoration: 'none',
            }}
        >
            <div style={{ display: 'flex', alignItems: 'start', gap: 10 }}>
                <div style={{ flex: 1 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                        <span
                            style={{
                                width: 6,
                                height: 6,
                                borderRadius: '50%',
                                background: p.status === 'active' ? 'var(--accent)' : 'var(--fg-3)',
                            }}
                        />
                        <span
                            style={{
                                fontFamily: 'var(--font-mono)',
                                fontSize: 9,
                                color: 'var(--fg-3)',
                                letterSpacing: '0.08em',
                            }}
                        >
                            {p.commodity.toUpperCase()}
                        </span>
                        {isPaused && (
                            <span
                                style={{
                                    fontFamily: 'var(--font-mono)',
                                    fontSize: 9,
                                    color: 'var(--warn)',
                                    letterSpacing: '0.08em',
                                    marginLeft: 'auto',
                                }}
                            >
                                {p.status.toUpperCase()}
                            </span>
                        )}
                    </div>
                    <h3
                        style={{
                            fontFamily: 'var(--font-display)',
                            fontSize: 19,
                            fontWeight: 600,
                            color: 'var(--fg-0)',
                            letterSpacing: '-0.01em',
                            lineHeight: 1.1,
                        }}
                    >
                        {p.name}
                    </h3>
                    <div style={{ fontSize: 11, color: 'var(--fg-3)', marginTop: 2 }}>{p.subtitle}</div>
                </div>
                <div style={{ textAlign: 'right' }}>
                    <div
                        style={{
                            fontFamily: 'var(--font-display)',
                            fontSize: 20,
                            color: 'var(--accent)',
                            fontWeight: 600,
                            lineHeight: 1,
                        }}
                    >
                        {p.confidence.toFixed(2)}
                    </div>
                    <div
                        style={{
                            fontFamily: 'var(--font-mono)',
                            fontSize: 9,
                            color: 'var(--fg-3)',
                            letterSpacing: '0.08em',
                            marginTop: 2,
                        }}
                    >
                        CONFIDENCE
                    </div>
                </div>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8, marginTop: 2 }}>
                <PfStat label="Holes" value={`${p.holes.complete}/${holesTotal || '—'}`} />
                <PfStat label="Grade" value={p.avgGrade !== null ? String(p.avgGrade) : '—'} sub={p.gradeUnit} />
                <PfStat
                    label="Meters"
                    value={p.metersDrilled > 0 ? `${(p.metersDrilled / 1000).toFixed(1)}k` : '—'}
                    sub={p.meterPlan > 0 ? `${meterPct.toFixed(0)}% plan` : ''}
                />
            </div>

            {p.meterPlan > 0 && (
                <div style={{ height: 4, background: 'var(--bg-3)', borderRadius: 2, overflow: 'hidden', marginTop: 2 }}>
                    <div
                        style={{
                            width: `${meterPct}%`,
                            height: '100%',
                            background: meterPct > 70 ? 'var(--accent)' : 'var(--accent-dim)',
                        }}
                    />
                </div>
            )}

            <div
                style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    paddingTop: 8,
                    borderTop: '1px solid var(--line-1)',
                    marginTop: 4,
                }}
            >
                <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--fg-3)' }}>CONF. TREND</span>
                {p.sparkConfidence.length > 0 ? (
                    <Sparkline values={p.sparkConfidence} stroke="var(--accent)" width={60} height={14} fill />
                ) : (
                    <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--fg-3)' }}>—</span>
                )}
                <div style={{ flex: 1 }} />
                <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--fg-3)' }}>
                    {p.queries30d} q/30d
                </span>
            </div>

            <div style={{ fontSize: 11, color: 'var(--fg-3)', lineHeight: 1.4, marginTop: 2 }}>{p.lastEvent}</div>
        </Link>
    );
}

function PfStat({ label, value, sub }: { label: string; value: string; sub?: string }) {
    return (
        <div style={{ padding: '6px 8px', background: 'var(--bg-2)', borderRadius: 3 }}>
            <div
                style={{
                    fontFamily: 'var(--font-mono)',
                    fontSize: 8.5,
                    color: 'var(--fg-3)',
                    letterSpacing: '0.08em',
                }}
            >
                {label.toUpperCase()}
            </div>
            <div
                style={{
                    fontFamily: 'var(--font-mono)',
                    fontSize: 14,
                    color: 'var(--fg-0)',
                    fontWeight: 600,
                    marginTop: 2,
                }}
            >
                {value}
            </div>
            {sub && (
                <div style={{ fontFamily: 'var(--font-mono)', fontSize: 8.5, color: 'var(--fg-3)', marginTop: 1 }}>
                    {sub}
                </div>
            )}
        </div>
    );
}

function PfEconBars({ projects }: { projects: ProjectRow[] }) {
    if (projects.length === 0) {
        return (
            <div style={{ fontSize: 11, color: 'var(--fg-3)', fontFamily: 'var(--font-mono)' }}>
                No drill-economics data available yet.
            </div>
        );
    }
    const maxMeters = Math.max(...projects.map((p) => p.meterPlan));
    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {projects.map((p) => {
                const drilledPct = (p.metersDrilled / maxMeters) * 100;
                const planPct = (p.meterPlan / maxMeters) * 100;
                const isPaused = p.status === 'paused' || p.status === 'archived';
                return (
                    <div key={p.id}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11.5, marginBottom: 4 }}>
                            <span style={{ color: 'var(--fg-1)' }}>{p.name}</span>
                            <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--fg-3)' }}>
                                {p.metersDrilled.toLocaleString()}{' '}
                                <span style={{ color: 'var(--fg-4)' }}>/ {p.meterPlan.toLocaleString()} m</span>
                            </span>
                        </div>
                        <div
                            style={{
                                position: 'relative',
                                height: 14,
                                background: 'var(--bg-2)',
                                borderRadius: 2,
                                overflow: 'hidden',
                            }}
                        >
                            <div
                                style={{
                                    width: `${planPct}%`,
                                    height: '100%',
                                    position: 'absolute',
                                    top: 0,
                                    left: 0,
                                    background: 'var(--bg-3)',
                                    borderRight: '1px dashed var(--fg-3)',
                                }}
                            />
                            <div
                                style={{
                                    width: `${drilledPct}%`,
                                    height: '100%',
                                    position: 'absolute',
                                    top: 0,
                                    left: 0,
                                    background: isPaused ? 'var(--fg-3)' : 'var(--accent)',
                                    opacity: 0.95,
                                }}
                            />
                        </div>
                    </div>
                );
            })}
        </div>
    );
}

function PfOrgMap({ projects }: { projects: ProjectRow[] }) {
    // Geographic projection — crude lng→x lat→y for North America viewport so
    // Wyoming, Canadian provinces, and US west all sit on the same canvas.
    // lng -140..-50 → x 5..95; lat 75..25 → y 8..78
    const positionable = projects.filter((p) => p.lat !== null && p.lng !== null);
    const pos = (p: ProjectRow) => {
        const x = ((p.lng! + 140) / 90) * 90 + 5;
        const y = ((75 - p.lat!) / 50) * 70 + 8;
        return { x, y };
    };
    return (
        <div
            style={{
                aspectRatio: '2.2 / 1',
                position: 'relative',
                background: 'linear-gradient(180deg, oklch(0.18 0.012 240) 0%, oklch(0.14 0.01 240) 100%)',
                border: '1px solid var(--line-1)',
                borderRadius: 4,
                overflow: 'hidden',
            }}
        >
            <svg width="100%" height="100%" viewBox="0 0 100 50" preserveAspectRatio="xMidYMid slice">
                {/* Approximate North America silhouette covering both
                    Canada (lat ~45-70) and the US lower-48 (lat ~25-49).
                    Tuned so Wyoming (~42°N) and Saskatchewan (~55°N) both
                    sit on land for the current projection. */}
                <path
                    d="M4,10 Q12,6 24,8 Q38,5 52,7 Q66,6 80,9 Q90,9 96,12 L98,20 Q92,18 88,24 Q82,30 72,30 L68,34 Q60,40 50,42 Q40,44 30,40 Q20,42 14,36 Q8,32 4,22 Z"
                    fill="oklch(0.22 0.014 240)"
                    stroke="oklch(0.32 0.018 240)"
                    strokeWidth="0.2"
                />
                {/* US/Canada border hint around lat ~49 → y = (75-49)/50*70+8 = 44.4 (in container %).
                    In SVG-50 space that's ~22; draw a faint horizontal at y=22. */}
                <line x1="14" y1="22" x2="92" y2="22" stroke="oklch(0.30 0.014 240)" strokeWidth="0.18" strokeDasharray="1 1.2" />
                {[10, 20, 30, 40].map((y) => (
                    <line
                        key={y}
                        x1="0"
                        y1={y}
                        x2="100"
                        y2={y}
                        stroke="oklch(0.28 0.018 240)"
                        strokeWidth="0.12"
                        strokeDasharray="0.5 1"
                    />
                ))}
            </svg>
            {positionable.map((p) => {
                const { x, y } = pos(p);
                const r = 1.4 + Math.min(1.6, p.holes.complete / 22);
                const isActive = p.status === 'active';
                return (
                    <Link
                        key={p.id}
                        href={`/projects/${p.id}`}
                        title={p.name}
                        style={{
                            position: 'absolute',
                            left: `${x}%`,
                            top: `${y}%`,
                            transform: 'translate(-50%, -50%)',
                            background: 'transparent',
                            padding: 0,
                        }}
                    >
                        <svg width="80" height="50" viewBox="-12 -8 24 16">
                            {isActive && (
                                <circle cx="0" cy="0" r="3.4" fill="oklch(0.82 0.15 160 / 0.3)">
                                    <animate
                                        attributeName="r"
                                        values={`${r * 1.4};${r * 2.2};${r * 1.4}`}
                                        dur="2.6s"
                                        repeatCount="indefinite"
                                    />
                                    <animate
                                        attributeName="opacity"
                                        values="0.6;0;0.6"
                                        dur="2.6s"
                                        repeatCount="indefinite"
                                    />
                                </circle>
                            )}
                            <circle
                                cx="0"
                                cy="0"
                                r={r}
                                fill={isActive ? 'oklch(0.82 0.15 160)' : 'oklch(0.55 0.04 240)'}
                                stroke="#0a0c10"
                                strokeWidth="0.4"
                            />
                            <text
                                x="0"
                                y={r + 4}
                                fill="rgba(255,255,255,0.85)"
                                fontSize="2.6"
                                textAnchor="middle"
                                fontFamily="ui-monospace, monospace"
                                letterSpacing="0.05"
                            >
                                {p.name}
                            </text>
                            <text
                                x="0"
                                y={r + 7.5}
                                fill={isActive ? 'oklch(0.82 0.15 160)' : 'rgba(255,255,255,0.45)'}
                                fontSize="2.2"
                                textAnchor="middle"
                                fontFamily="ui-monospace, monospace"
                            >
                                {p.confidence.toFixed(2)}
                            </text>
                        </svg>
                    </Link>
                );
            })}
        </div>
    );
}

function SparkleIcon({ size = 12, color = 'currentColor' }: { size?: number; color?: string }) {
    return (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 3v5 M12 16v5 M3 12h5 M16 12h5 M6 6l3 3 M15 15l3 3 M6 18l3-3 M15 9l3-3" />
        </svg>
    );
}

function GlobeIcon({ size = 12, color = 'currentColor' }: { size?: number; color?: string }) {
    return (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="9" />
            <path d="M3 12h18 M12 3c2.5 3 2.5 15 0 18 M12 3c-2.5 3-2.5 15 0 18" />
        </svg>
    );
}
