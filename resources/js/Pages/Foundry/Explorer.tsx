import { useState } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, Segmented, EmptyState, StatusDot } from '@/Components/Foundry/primitives';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';

interface Collar {
    collar_id: string;
    hole_id: string;
    hole_id_canonical: string;
    total_depth: number | null;
    status: string;
    easting: number | null;
    northing: number | null;
}

interface ExplorerProps {
    project: { project_id: string; project_name: string; slug: string };
    collars: Collar[];
    detail: {
        collar: Record<string, unknown>;
        lithology: Array<{ from_depth: number; to_depth: number; code: string; description: string; color: string | null }>;
        samples: Array<{ sample_id: string; from_depth: number; to_depth: number; type: string; assays: Record<string, unknown> }>;
    } | null;
    filters: { status: string | null; search: string | null; active_hole: string | null };
    empty: boolean;
}

type Tab = 'map' | 'strip' | 'analysis' | '3d';

export default function FoundryExplorer({ project, collars, detail, filters, empty }: ExplorerProps) {
    const [tab, setTab] = useState<Tab>('map');
    const [search, setSearch] = useState(filters.search ?? '');
    const [showDetail, setShowDetail] = useState<boolean>(Boolean(filters.active_hole));

    // Reliability spec Phase 2b — Explorer/Map sources collars off
    // silver.collars + structures/assays for the detail sheet. On a
    // workspace.data_updated event that touches any of those, partial
    // reload of the relevant props. The MVT tile layer carries its own
    // ETag scoped to data_version (TileProxyController) — bumping
    // data_version in the controller forces fresh tiles automatically
    // on the next render, no MapLibre setTiles() needed here.
    useWorkspaceDataUpdated(project.project_id, (evt) => {
        const t = evt.affected_types;
        if (t.includes('collars') || t.includes('assays') || t.includes('reports')) {
            router.reload({ only: ['collars', 'detail', 'filters', 'empty'] });
        }
    });

    function selectHole(hole: string) {
        router.get(`/projects/${project.slug}/explorer`, {
            ...(filters.status ? { status: filters.status } : {}),
            ...(search ? { q: search } : {}),
            hole,
        }, { preserveState: true });
        setShowDetail(true);
    }

    function applySearch() {
        router.get(`/projects/${project.slug}/explorer`, {
            ...(filters.status ? { status: filters.status } : {}),
            q: search || undefined,
        }, { preserveState: true });
    }

    return (
        <AppLayout>
            <Head title={`Explorer · ${project.project_name}`} />

            <div className="flex-1 grid grid-cols-[300px_1fr] overflow-hidden" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                {/* Hole browser left rail */}
                <aside className="border-r overflow-hidden flex flex-col" style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}>
                    <div className="px-3 py-3 border-b shrink-0 space-y-2" style={{ borderColor: 'var(--line-1)' }}>
                        <div className="text-[10px] font-mono uppercase tracking-[0.12em]" style={{ color: 'var(--fg-3)' }}>Drill holes · {collars.length}</div>
                        <input
                            type="text"
                            placeholder="Search hole ID…"
                            value={search}
                            onChange={(e) => setSearch(e.target.value)}
                            onKeyDown={(e) => e.key === 'Enter' && applySearch()}
                            className="w-full text-xs px-2 py-1.5 rounded border"
                            style={{ background: 'var(--bg-2)', color: 'var(--fg-0)', borderColor: 'var(--line-2)' }}
                        />
                    </div>
                    <div className="flex-1 overflow-y-auto">
                        {empty ? (
                            <div className="px-3 py-6 text-center text-xs" style={{ color: 'var(--fg-3)' }}>No drill holes ingested yet.</div>
                        ) : collars.map((c) => (
                            <button
                                key={c.collar_id}
                                type="button"
                                onClick={() => selectHole(c.hole_id_canonical)}
                                className="w-full text-left px-3 py-2 border-b transition-colors"
                                style={{
                                    borderColor: 'var(--line-1)',
                                    background: filters.active_hole === c.hole_id_canonical ? 'var(--accent-bg)' : 'transparent',
                                }}
                            >
                                <div className="flex items-center gap-2">
                                    <StatusDot status={c.status} />
                                    <span className="text-xs font-mono" style={{ color: 'var(--fg-0)' }}>{c.hole_id_canonical}</span>
                                </div>
                                <div className="text-[10px] font-mono uppercase tracking-wider mt-0.5" style={{ color: 'var(--fg-3)' }}>
                                    {c.total_depth !== null ? `${c.total_depth.toFixed(0)}m` : '—'} · {c.status}
                                </div>
                            </button>
                        ))}
                    </div>
                </aside>

                {/* Main canvas */}
                <section className="flex flex-col overflow-hidden">
                    <PageHeader
                        eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · EXPLORER`}
                        title={filters.active_hole ?? 'Drill explorer'}
                        sub={`${collars.length} hole${collars.length === 1 ? '' : 's'}`}
                        actions={
                            <Segmented<Tab>
                                value={tab}
                                onChange={setTab}
                                options={[
                                    { value: 'map', label: 'Map' },
                                    { value: 'strip', label: 'Strip log' },
                                    { value: 'analysis', label: 'Analysis' },
                                    { value: '3d', label: '3D' },
                                ]}
                            />
                        }
                    />

                    <div className="flex-1 overflow-y-auto p-6">
                        {tab === 'map' && (
                            <MapPlaceholder collars={collars} active={filters.active_hole} onPick={selectHole} />
                        )}
                        {tab === 'strip' && detail && <StripPanel detail={detail} />}
                        {tab === 'strip' && !detail && <EmptyState title="Select a hole to view its strip log." />}
                        {tab === 'analysis' && detail && <AnalysisPanel detail={detail} />}
                        {tab === 'analysis' && !detail && <EmptyState title="Select a hole to view assays + geochem analysis." />}
                        {tab === '3d' && (
                            <Card eyebrow="3D" title="Borehole 3D viewer">
                                <div className="text-xs" style={{ color: 'var(--fg-2)' }}>3D borehole rendering reads silver.drill_traces + gold_drillhole_intervals_visual. View component wraps MapLibre + Three.js (deferred to next slice).</div>
                            </Card>
                        )}
                    </div>
                </section>
            </div>

            {/* Hole detail slide-over */}
            {showDetail && detail && (
                <HoleDetailSheet detail={detail} onClose={() => setShowDetail(false)} />
            )}
        </AppLayout>
    );
}

function MapPlaceholder({ collars, active, onPick }: { collars: Collar[]; active: string | null; onPick: (h: string) => void }) {
    const minE = Math.min(...collars.map((c) => c.easting ?? 0)) || 0;
    const maxE = Math.max(...collars.map((c) => c.easting ?? 1)) || 1;
    const minN = Math.min(...collars.map((c) => c.northing ?? 0)) || 0;
    const maxN = Math.max(...collars.map((c) => c.northing ?? 1)) || 1;
    const w = Math.max(1, maxE - minE);
    const h = Math.max(1, maxN - minN);

    return (
        <Card eyebrow="MAP · UTM Z13N" title="Collar layout">
            <svg width="100%" height="480" viewBox="0 0 100 100" preserveAspectRatio="xMidYMid meet" style={{ background: 'var(--bg-2)' }}>
                {[20, 40, 60, 80].map((p) => (
                    <line key={p} x1="0" x2="100" y1={p} y2={p} stroke="var(--line-1)" strokeWidth="0.1" strokeDasharray="0.5 0.5" />
                ))}
                {[20, 40, 60, 80].map((p) => (
                    <line key={`v${p}`} x1={p} x2={p} y1="0" y2="100" stroke="var(--line-1)" strokeWidth="0.1" strokeDasharray="0.5 0.5" />
                ))}
                {collars.map((c) => {
                    if (c.easting === null || c.northing === null) return null;
                    const x = ((c.easting - minE) / w) * 90 + 5;
                    const y = 95 - ((c.northing - minN) / h) * 90;
                    const isActive = c.hole_id_canonical === active;
                    return (
                        <g key={c.collar_id} onClick={() => onPick(c.hole_id_canonical)} style={{ cursor: 'pointer' }}>
                            <circle cx={x} cy={y} r={isActive ? 1.4 : 0.9} fill={isActive ? 'var(--accent)' : 'var(--fg-2)'} />
                        </g>
                    );
                })}
            </svg>
            <div className="text-[10px] font-mono uppercase tracking-wider mt-2" style={{ color: 'var(--fg-3)' }}>
                Click a collar to drop into the strip log. MapLibre overlay deferred to next slice — this is the working SVG canvas with real easting/northing.
            </div>
        </Card>
    );
}

function StripPanel({ detail }: { detail: NonNullable<ExplorerProps['detail']> }) {
    const totalDepth = detail.lithology.length > 0 ? detail.lithology[detail.lithology.length - 1].to_depth : 0;
    return (
        <Card eyebrow="STRIP LOG" title={`${detail.lithology.length} intervals · ${totalDepth.toFixed(0)}m`}>
            <div className="grid grid-cols-[60px_120px_1fr_120px] gap-3 text-[10px] font-mono uppercase tracking-wider mb-2 px-2" style={{ color: 'var(--fg-3)' }}>
                <div>Depth</div>
                <div>Code</div>
                <div>Description</div>
                <div>Color</div>
            </div>
            <div className="max-h-[520px] overflow-y-auto">
                {detail.lithology.map((l, i) => (
                    <div key={i} className="grid grid-cols-[60px_120px_1fr_120px] gap-3 text-xs px-2 py-1.5 border-b" style={{ borderColor: 'var(--line-1)' }}>
                        <span className="font-mono" style={{ color: 'var(--fg-2)' }}>{l.from_depth.toFixed(1)}–{l.to_depth.toFixed(1)}</span>
                        <span className="font-mono" style={{ color: 'var(--fg-0)' }}>{l.code}</span>
                        <span style={{ color: 'var(--fg-1)' }}>{l.description}</span>
                        <span className="font-mono" style={{ color: 'var(--fg-3)' }}>{l.color ?? '—'}</span>
                    </div>
                ))}
            </div>
        </Card>
    );
}

function AnalysisPanel({ detail }: { detail: NonNullable<ExplorerProps['detail']> }) {
    return (
        <Card eyebrow="ANALYSIS" title={`${detail.samples.length} samples`}>
            {detail.samples.length === 0 ? (
                <div className="text-xs" style={{ color: 'var(--fg-3)' }}>No assays recorded for this hole.</div>
            ) : (
                <div className="grid grid-cols-[60px_80px_1fr] gap-3 text-xs">
                    <div className="font-mono uppercase text-[10px]" style={{ color: 'var(--fg-3)' }}>Depth</div>
                    <div className="font-mono uppercase text-[10px]" style={{ color: 'var(--fg-3)' }}>Type</div>
                    <div className="font-mono uppercase text-[10px]" style={{ color: 'var(--fg-3)' }}>Assays</div>
                    {detail.samples.map((s) => (
                        <>
                            <span key={`d-${s.sample_id}`} className="font-mono" style={{ color: 'var(--fg-2)' }}>{s.from_depth.toFixed(1)}–{s.to_depth.toFixed(1)}</span>
                            <span key={`t-${s.sample_id}`} className="font-mono" style={{ color: 'var(--fg-0)' }}>{s.type}</span>
                            <span key={`a-${s.sample_id}`} className="font-mono text-[11px]" style={{ color: 'var(--fg-1)' }}>{JSON.stringify(s.assays).slice(0, 140)}</span>
                        </>
                    ))}
                </div>
            )}
        </Card>
    );
}

function HoleDetailSheet({ detail, onClose }: { detail: NonNullable<ExplorerProps['detail']>; onClose: () => void }) {
    const c = detail.collar as { hole_id: string; total_depth?: number; status?: string };
    return (
        <div className="fixed inset-0 z-[100]" style={{ background: 'rgba(8,10,14,0.6)' }} onClick={onClose}>
            <aside className="absolute right-0 top-0 bottom-0 w-[420px] flex flex-col foundry" style={{ background: 'var(--bg-1)', borderLeft: '1px solid var(--line-2)' }} onClick={(e) => e.stopPropagation()}>
                <header className="px-4 py-3 border-b flex items-center" style={{ borderColor: 'var(--line-1)' }}>
                    <div className="flex-1">
                        <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>HOLE DETAIL</div>
                        <div className="text-sm font-mono mt-0.5" style={{ color: 'var(--fg-0)' }}>{c.hole_id}</div>
                    </div>
                    <button type="button" onClick={onClose} className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border" style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}>Close ×</button>
                </header>
                <div className="flex-1 overflow-y-auto p-4 space-y-3">
                    <Pill tone="accent" dot>{c.status ?? 'unknown'}</Pill>
                    <div className="text-xs grid grid-cols-2 gap-2">
                        <Field label="Total depth" value={c.total_depth !== undefined && c.total_depth !== null ? `${c.total_depth} m` : '—'} />
                        <Field label="Lithology intervals" value={String(detail.lithology.length)} />
                        <Field label="Samples" value={String(detail.samples.length)} />
                    </div>
                    <Card eyebrow="STRIP" title="First 10 intervals" padded={false}>
                        {detail.lithology.slice(0, 10).map((l, i) => (
                            <div key={i} className="grid grid-cols-[60px_1fr] gap-2 text-[11px] font-mono px-3 py-1 border-b" style={{ borderColor: 'var(--line-1)', color: 'var(--fg-1)' }}>
                                <span style={{ color: 'var(--fg-3)' }}>{l.from_depth.toFixed(0)}–{l.to_depth.toFixed(0)}m</span>
                                <span>{l.code} {l.description}</span>
                            </div>
                        ))}
                    </Card>
                </div>
            </aside>
        </div>
    );
}

function Field({ label, value }: { label: string; value: string }) {
    return (
        <div className="px-2 py-1.5 rounded-sm" style={{ background: 'var(--bg-2)' }}>
            <div className="text-[9px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>{label}</div>
            <div className="text-xs font-mono mt-0.5" style={{ color: 'var(--fg-0)' }}>{value}</div>
        </div>
    );
}
