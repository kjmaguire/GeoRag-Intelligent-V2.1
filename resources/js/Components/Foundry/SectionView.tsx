import { useEffect, useMemo, useState } from 'react';
import { LithologyStripColumn, type LithologyInterval } from '@/Components/Foundry/Charts';

interface HolePayload {
    hole_id: string;
    total_depth: number | null;
    easting: number | null;
    northing: number | null;
    lat: number | null;
    lng: number | null;
    lithology_intervals: LithologyInterval[];
    ore_bands: number;
    ore_thickness_m: number;
}

type FetchState =
    | { kind: 'loading' }
    | { kind: 'error'; message: string }
    | { kind: 'ready'; payload: HolePayload };

async function fetchHolePayload(slug: string, holeId: string): Promise<HolePayload> {
    const r = await fetch(`/projects/${slug}/holes/${encodeURIComponent(holeId)}/payload`, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
}

function haversineM(a: { lat: number; lng: number }, b: { lat: number; lng: number }): number {
    const R = 6371000;
    const toRad = (d: number) => (d * Math.PI) / 180;
    const dLat = toRad(b.lat - a.lat);
    const dLng = toRad(b.lng - a.lng);
    const φ1 = toRad(a.lat);
    const φ2 = toRad(b.lat);
    const x = Math.sin(dLat / 2) ** 2 + Math.cos(φ1) * Math.cos(φ2) * Math.sin(dLng / 2) ** 2;
    return 2 * R * Math.atan2(Math.sqrt(x), Math.sqrt(1 - x));
}

/**
 * SectionView — ad-hoc cross-section between two collars.
 *
 * No persistence yet (gold.cross_section_panels stays at 0). Just picks
 * two holes, fetches each one's derived lithology via the existing
 * /projects/{slug}/holes/{hole}/payload endpoint, renders them on a
 * shared depth axis with the inter-hole horizontal distance labelled
 * between them. Compass azimuth from A → B included for orientation
 * context.
 */
export function SectionView({
    projectSlug,
    holeOptions,
    defaultLeft,
    defaultRight,
    chartH,
}: {
    projectSlug: string;
    holeOptions: string[];
    defaultLeft?: string;
    defaultRight?: string;
    chartH: number;
}) {
    const [leftId, setLeftId] = useState<string>(defaultLeft ?? holeOptions[0] ?? '');
    const [rightId, setRightId] = useState<string>(defaultRight ?? holeOptions[1] ?? holeOptions[0] ?? '');
    const [left, setLeft] = useState<FetchState>({ kind: 'loading' });
    const [right, setRight] = useState<FetchState>({ kind: 'loading' });

    useEffect(() => {
        if (!leftId) return;
        let cancelled = false;
        setLeft({ kind: 'loading' });
        fetchHolePayload(projectSlug, leftId)
            .then((p) => !cancelled && setLeft({ kind: 'ready', payload: p }))
            .catch((e) => !cancelled && setLeft({ kind: 'error', message: e.message ?? 'fetch failed' }));
        return () => { cancelled = true; };
    }, [projectSlug, leftId]);

    useEffect(() => {
        if (!rightId) return;
        let cancelled = false;
        setRight({ kind: 'loading' });
        fetchHolePayload(projectSlug, rightId)
            .then((p) => !cancelled && setRight({ kind: 'ready', payload: p }))
            .catch((e) => !cancelled && setRight({ kind: 'error', message: e.message ?? 'fetch failed' }));
        return () => { cancelled = true; };
    }, [projectSlug, rightId]);

    const { distanceM, azimuthDeg } = useMemo(() => {
        if (left.kind !== 'ready' || right.kind !== 'ready') return { distanceM: null as number | null, azimuthDeg: null as number | null };
        const L = left.payload; const R = right.payload;
        if (L.lat === null || L.lng === null || R.lat === null || R.lng === null) return { distanceM: null, azimuthDeg: null };
        const d = haversineM({ lat: L.lat, lng: L.lng }, { lat: R.lat, lng: R.lng });
        // Compass azimuth A→B
        const φ1 = (L.lat * Math.PI) / 180; const φ2 = (R.lat * Math.PI) / 180;
        const dλ = ((R.lng - L.lng) * Math.PI) / 180;
        const y = Math.sin(dλ) * Math.cos(φ2);
        const x = Math.cos(φ1) * Math.sin(φ2) - Math.sin(φ1) * Math.cos(φ2) * Math.cos(dλ);
        let bearing = (Math.atan2(y, x) * 180) / Math.PI;
        bearing = (bearing + 360) % 360;
        return { distanceM: d, azimuthDeg: bearing };
    }, [left, right]);

    const sharedDepthMax = Math.max(
        left.kind === 'ready' ? (left.payload.total_depth ?? 0) : 0,
        right.kind === 'ready' ? (right.payload.total_depth ?? 0) : 0,
        100,
    );

    function HolePicker({ value, onChange, side }: { value: string; onChange: (v: string) => void; side: string }) {
        return (
            <div className="flex flex-col gap-1">
                <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>{side}</span>
                <select
                    value={value}
                    onChange={(e) => onChange(e.target.value)}
                    className="text-[11px] font-mono px-2 py-1 rounded border"
                    style={{ borderColor: 'var(--line-2)', color: 'var(--fg-1)', background: 'var(--bg-2)' }}
                >
                    {holeOptions.map((h) => (<option key={h} value={h}>{h}</option>))}
                </select>
            </div>
        );
    }

    return (
        <div className="flex flex-col gap-3 min-h-0">
            <div className="flex items-end gap-6 flex-wrap shrink-0">
                <HolePicker value={leftId} onChange={setLeftId} side="LEFT" />
                <div className="text-center px-3 py-2 rounded border" style={{ borderColor: 'var(--line-1)', background: 'var(--bg-2)', minWidth: 180 }}>
                    <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                        Inter-hole
                    </div>
                    <div className="text-sm font-mono" style={{ color: 'var(--fg-0)' }}>
                        {distanceM !== null ? (distanceM >= 1000 ? `${(distanceM / 1000).toFixed(2)} km` : `${Math.round(distanceM)} m`) : '—'}
                    </div>
                    {azimuthDeg !== null && (
                        <div className="text-[10px] font-mono" style={{ color: 'var(--fg-2)' }}>
                            Azimuth A→B {azimuthDeg.toFixed(0)}°
                        </div>
                    )}
                </div>
                <HolePicker value={rightId} onChange={setRightId} side="RIGHT" />
            </div>

            <div className="flex gap-6 items-start overflow-x-auto flex-1 min-h-0">
                {[left, right].map((state, i) => {
                    const label = i === 0 ? leftId : rightId;
                    return (
                        <div key={label + i} className="shrink-0 flex flex-col gap-2">
                            <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                                {i === 0 ? 'LEFT · ' : 'RIGHT · '}{label}
                            </div>
                            {state.kind === 'loading' && (
                                <div className="text-xs" style={{ color: 'var(--fg-3)' }}>Loading…</div>
                            )}
                            {state.kind === 'error' && (
                                <div className="text-xs" style={{ color: '#d97706' }}>Failed: {state.message}</div>
                            )}
                            {state.kind === 'ready' && (
                                <LithologyStripColumn
                                    intervals={state.payload.lithology_intervals}
                                    holeId={state.payload.hole_id}
                                    depthMax={sharedDepthMax}
                                    height={chartH}
                                    width={320}
                                />
                            )}
                        </div>
                    );
                })}
            </div>
            <div className="text-[10px] font-mono shrink-0" style={{ color: 'var(--fg-3)' }}>
                Ad-hoc section — derived from each hole's lithology bands.
                gold.cross_section_panels has 0 rows; this view doesn't persist.
                Click a hole on the MAP to set up a section, or pick from the dropdowns above.
            </div>
        </div>
    );
}
