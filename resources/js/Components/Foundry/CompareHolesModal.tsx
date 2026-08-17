import { useEffect, useMemo, useState } from 'react';
import { DownholeMultiLog, LithologyStripColumn, type LithologyInterval } from '@/Components/Foundry/Charts';

interface HolePayload {
    hole_id: string;
    collar_id: string;
    total_depth: number | null;
    easting: number | null;
    northing: number | null;
    lat: number | null;
    lng: number | null;
    log_tracks: Array<{
        label: string;
        color: string;
        points: Array<{ depth: number; value: number }>;
        min: number;
        max: number;
    }>;
    log_depth_max: number;
    lithology_intervals: LithologyInterval[];
    ore_bands: number;
    ore_thickness_m: number;
    mean_u3o8_pct: number | null;
}

type FetchState =
    | { kind: 'loading' }
    | { kind: 'error'; message: string }
    | { kind: 'ready'; payload: HolePayload };

async function fetchHolePayload(slug: string, holeId: string): Promise<HolePayload> {
    const r = await fetch(`/projects/${slug}/holes/${encodeURIComponent(holeId)}/payload`, {
        credentials: 'same-origin',
        headers: {
            Accept: 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
        },
    });
    if (!r.ok) {
        throw new Error(`HTTP ${r.status}`);
    }
    return r.json();
}

export function CompareHolesModal({
    projectSlug,
    leftHole,
    rightHole,
    onClose,
}: {
    projectSlug: string;
    leftHole: string;
    rightHole: string;
    onClose: () => void;
}) {
    const [left, setLeft] = useState<FetchState>({ kind: 'loading' });
    const [right, setRight] = useState<FetchState>({ kind: 'loading' });

    useEffect(() => {
        let cancelled = false;
        fetchHolePayload(projectSlug, leftHole)
            .then((p) => !cancelled && setLeft({ kind: 'ready', payload: p }))
            .catch((e) => !cancelled && setLeft({ kind: 'error', message: e.message ?? 'fetch failed' }));
        fetchHolePayload(projectSlug, rightHole)
            .then((p) => !cancelled && setRight({ kind: 'ready', payload: p }))
            .catch((e) => !cancelled && setRight({ kind: 'error', message: e.message ?? 'fetch failed' }));
        return () => {
            cancelled = true;
        };
    }, [projectSlug, leftHole, rightHole]);

    // Shared depth axis so both holes plot at the same scale.
    const depthMax = Math.max(
        left.kind === 'ready' ? left.payload.log_depth_max : 0,
        right.kind === 'ready' ? right.payload.log_depth_max : 0,
        600,
    );

    // Chart height tied to modal viewport so charts breathe on tall windows
    // without forcing a page scroll on short ones.
    const [windowHeight, setWindowHeight] = useState<number>(() =>
        typeof window === 'undefined' ? 820 : window.innerHeight,
    );
    useEffect(() => {
        function onResize() {
            setWindowHeight(window.innerHeight);
        }
        window.addEventListener('resize', onResize);
        return () => window.removeEventListener('resize', onResize);
    }, []);
    const chartH = useMemo(() => Math.max(360, windowHeight - 360), [windowHeight]);

    return (
        <div
            className="fixed inset-0 z-50 flex items-center justify-center p-4"
            style={{ background: 'rgba(2,5,10,0.72)' }}
            onClick={onClose}
        >
            <div
                className="rounded-lg border overflow-hidden flex flex-col"
                style={{
                    background: 'var(--bg-0)',
                    borderColor: 'var(--line-1)',
                    color: 'var(--fg-1)',
                    width: 'min(1600px, 98vw)',
                    height: '96vh',
                }}
                onClick={(e) => e.stopPropagation()}
            >
                <div className="flex items-center justify-between px-6 py-3 border-b shrink-0" style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}>
                    <div>
                        <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>Hole comparison</div>
                        <div className="text-sm font-medium" style={{ color: 'var(--fg-0)' }}>
                            {leftHole} <span style={{ color: 'var(--fg-3)' }}>vs</span> {rightHole}
                        </div>
                    </div>
                    <button
                        type="button"
                        onClick={onClose}
                        className="text-[11px] font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                        style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)', background: 'var(--bg-2)' }}
                    >
                        Close ✕
                    </button>
                </div>

                <div className="flex-1 overflow-y-auto p-6">
                    <DiffStats left={left} right={right} />

                    <div className="grid grid-cols-2 gap-6 mt-6">
                        {[left, right].map((state, i) => {
                            const label = i === 0 ? leftHole : rightHole;
                            return (
                                <div key={label} className="space-y-3">
                                    <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                                        {i === 0 ? 'LEFT' : 'RIGHT'} · {label}
                                    </div>
                                    {state.kind === 'loading' && (
                                        <div className="text-xs" style={{ color: 'var(--fg-3)' }}>Loading hole payload…</div>
                                    )}
                                    {state.kind === 'error' && (
                                        <div className="text-xs" style={{ color: 'var(--warn, #d97706)' }}>
                                            Failed to load: {state.message}
                                        </div>
                                    )}
                                    {state.kind === 'ready' && (
                                        <HoleColumn payload={state.payload} depthMax={depthMax} chartH={chartH} />
                                    )}
                                </div>
                            );
                        })}
                    </div>
                </div>
            </div>
        </div>
    );
}

function DiffStats({ left, right }: { left: FetchState; right: FetchState }) {
    if (left.kind !== 'ready' || right.kind !== 'ready') {
        return (
            <div
                className="text-[11px] font-mono px-3 py-2 rounded border"
                style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)', color: 'var(--fg-3)' }}
            >
                Loading comparison stats…
            </div>
        );
    }
    const L = left.payload;
    const R = right.payload;
    const rows: Array<{ label: string; l: string; r: string; tone?: 'highlight' }> = [
        { label: 'Total depth', l: L.total_depth !== null ? `${L.total_depth.toFixed(1)} m` : '—', r: R.total_depth !== null ? `${R.total_depth.toFixed(1)} m` : '—' },
        { label: 'Derived ore bands', l: String(L.ore_bands), r: String(R.ore_bands), tone: L.ore_bands > 0 || R.ore_bands > 0 ? 'highlight' : undefined },
        { label: 'U-host thickness', l: `${L.ore_thickness_m.toFixed(1)} m`, r: `${R.ore_thickness_m.toFixed(1)} m`, tone: 'highlight' },
        { label: 'Mean U₃O₈ (eU)', l: L.mean_u3o8_pct !== null ? `${(L.mean_u3o8_pct * 100).toFixed(3)}%` : '—', r: R.mean_u3o8_pct !== null ? `${(R.mean_u3o8_pct * 100).toFixed(3)}%` : '—' },
        { label: 'Curves rendered', l: String(L.log_tracks.length), r: String(R.log_tracks.length) },
        { label: 'Lithology bands', l: String(L.lithology_intervals.length), r: String(R.lithology_intervals.length) },
        { label: 'Easting (UTM 13N)', l: L.easting !== null ? Math.round(L.easting).toLocaleString() : '—', r: R.easting !== null ? Math.round(R.easting).toLocaleString() : '—' },
        { label: 'Northing (UTM 13N)', l: L.northing !== null ? Math.round(L.northing).toLocaleString() : '—', r: R.northing !== null ? Math.round(R.northing).toLocaleString() : '—' },
    ];
    return (
        <div className="rounded border overflow-hidden" style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}>
            <div className="grid grid-cols-[1fr_1fr_1fr] text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)', background: 'var(--bg-2)' }}>
                <div className="px-3 py-1.5">Metric</div>
                <div className="px-3 py-1.5">Left · {L.hole_id}</div>
                <div className="px-3 py-1.5">Right · {R.hole_id}</div>
            </div>
            {rows.map((row) => (
                <div
                    key={row.label}
                    className="grid grid-cols-[1fr_1fr_1fr] text-xs border-t"
                    style={{ borderColor: 'var(--line-1)' }}
                >
                    <div className="px-3 py-1.5" style={{ color: 'var(--fg-3)' }}>{row.label}</div>
                    <div className="px-3 py-1.5 font-mono" style={{ color: row.tone === 'highlight' ? '#8fe28b' : 'var(--fg-1)' }}>{row.l}</div>
                    <div className="px-3 py-1.5 font-mono" style={{ color: row.tone === 'highlight' ? '#8fe28b' : 'var(--fg-1)' }}>{row.r}</div>
                </div>
            ))}
        </div>
    );
}

function HoleColumn({ payload, depthMax, chartH }: { payload: HolePayload; depthMax: number; chartH: number }) {
    return (
        <div className="flex gap-5 items-start overflow-x-auto">
            <div className="shrink-0">
                <DownholeMultiLog tracks={payload.log_tracks} depthMax={depthMax} height={chartH} trackWidth={70} />
            </div>
            <div className="shrink-0">
                <LithologyStripColumn
                    intervals={payload.lithology_intervals}
                    holeId={payload.hole_id}
                    depthMax={depthMax}
                    height={chartH}
                    width={360}
                />
            </div>
        </div>
    );
}
