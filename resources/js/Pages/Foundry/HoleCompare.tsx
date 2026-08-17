import { useState } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, EmptyState } from '@/Components/Foundry/primitives';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';
import type { HoleCompareProps, CompareHoleDetail } from '@/Types/Foundry';

/**
 * Foundry HoleCompare — side-by-side comparison of two real drill holes
 * from the active project's silver.collars (+ lithology + samples).
 */
export default function FoundryHoleCompare({ project, pickable, left, right, empty }: HoleCompareProps) {
    // Phase 5 real-time push — drill-upload / ingest_pdf write new holes
    // that affect the pickable list + the left/right detail panes.
    useWorkspaceDataUpdated(project.project_id, (event) => {
        if (event.affected_types.includes('collars') || event.affected_types.includes('assays')) {
            router.reload({ only: ['pickable', 'left', 'right', 'empty'] });
        }
    });

    const [leftId, setLeftId] = useState<string>(left?.hole_id_canonical ?? left?.hole_id ?? '');
    const [rightId, setRightId] = useState<string>(right?.hole_id_canonical ?? right?.hole_id ?? '');

    function applyPair(l: string, r: string) {
        router.get(`/projects/${project.slug}/compare`, { left: l, right: r }, { preserveState: true });
    }

    return (
        <AppLayout>
            <Head title={`Compare · ${project.project_name}`} />

            <div className="flex-1 overflow-y-auto" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · COMPARE`}
                    title="Hole-vs-hole comparison"
                    sub={`${pickable.length} pickable hole${pickable.length === 1 ? '' : 's'} in this project`}
                />

                <div className="px-8 py-4 flex items-center gap-3">
                    <HolePicker label="LEFT" value={leftId} options={pickable} onChange={setLeftId} />
                    <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>vs</span>
                    <HolePicker label="RIGHT" value={rightId} options={pickable} onChange={setRightId} />
                    <button
                        type="button"
                        onClick={() => applyPair(leftId, rightId)}
                        disabled={!leftId || !rightId}
                        className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border disabled:opacity-40"
                        style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                    >
                        Compare
                    </button>
                </div>

                {empty ? (
                    <div className="px-8 py-12">
                        <EmptyState
                            title="No drill holes in this project yet."
                            detail="Ingest at least two collars before this surface can compare them. Use Data → Connect Source to add drill logs."
                        />
                    </div>
                ) : !left || !right ? (
                    <div className="px-8 py-12">
                        <EmptyState
                            title="Pick two holes to compare."
                            detail="Choose any two from the dropdowns above and click Compare."
                        />
                    </div>
                ) : (
                    <section className="px-8 pb-8 grid grid-cols-1 lg:grid-cols-2 gap-4">
                        <HoleColumn hole={left} />
                        <HoleColumn hole={right} />
                    </section>
                )}
            </div>
        </AppLayout>
    );
}

function HolePicker({ label, value, options, onChange }: {
    label: string;
    value: string;
    options: Array<{ hole_id: string; hole_id_canonical: string | null }>;
    onChange: (v: string) => void;
}) {
    return (
        <label className="flex items-center gap-2">
            <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>{label}</span>
            <select
                value={value}
                onChange={(e) => onChange(e.target.value)}
                className="px-2 py-1 text-xs font-mono rounded border"
                style={{ background: 'var(--bg-2)', color: 'var(--fg-0)', borderColor: 'var(--line-2)' }}
            >
                <option value="">— pick hole —</option>
                {options.map((o) => (
                    <option key={o.hole_id} value={o.hole_id_canonical ?? o.hole_id}>
                        {o.hole_id_canonical ?? o.hole_id}
                    </option>
                ))}
            </select>
        </label>
    );
}

function HoleColumn({ hole }: { hole: CompareHoleDetail }) {
    return (
        <Card eyebrow={`HOLE · ${hole.hole_id_canonical ?? hole.hole_id}`} title={hole.hole_id_canonical ?? hole.hole_id}>
            <div className="grid grid-cols-2 gap-3 text-xs">
                <Field label="Total depth (m)" value={hole.total_depth?.toFixed(1) ?? '—'} />
                <Field label="Status" value={hole.status ?? '—'} />
                <Field label="Lat / Lng" value={hole.latitude && hole.longitude ? `${hole.latitude.toFixed(4)}°, ${hole.longitude.toFixed(4)}°` : '—'} />
                <Field label="PLSS" value={hole.plss_section ?? '—'} />
                <Field label="UTM Z" value={hole.utm_zone ? `Z${hole.utm_zone}` : '—'} />
                <Field label="State plane (E / N)" value={hole.state_plane_easting && hole.state_plane_northing ? `${hole.state_plane_easting.toFixed(0)} / ${hole.state_plane_northing.toFixed(0)}` : '—'} />
                <Field label="Azimuth / Dip" value={hole.azimuth !== null && hole.dip !== null ? `${hole.azimuth}° / ${hole.dip}°` : '—'} />
                <Field label="Completed" value={hole.completed_at ?? '—'} />
            </div>

            {hole.lithology.length > 0 && (
                <div className="mt-4">
                    <div className="text-[10px] font-mono uppercase tracking-wider mb-2" style={{ color: 'var(--fg-3)' }}>
                        Lithology · {hole.lithology.length} intervals
                    </div>
                    <ul className="text-[11px] font-mono space-y-1 max-h-72 overflow-y-auto">
                        {hole.lithology.map((l, i) => (
                            <li key={i} className="flex items-baseline gap-2" style={{ color: 'var(--fg-1)' }}>
                                <span style={{ color: 'var(--fg-3)' }}>{l.from_depth.toFixed(1)}–{l.to_depth.toFixed(1)}m</span>
                                <span>{l.kind}</span>
                            </li>
                        ))}
                    </ul>
                </div>
            )}
        </Card>
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
