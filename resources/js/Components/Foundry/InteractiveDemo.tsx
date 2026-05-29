import { useState, useRef } from 'react';

/**
 * InteractiveDemo — 5 interactive widgets from the prototype.
 *   SectionAzimuthScrubber — 0-180° compass puck
 *   StereonetBrush — lasso pole selection
 *   DrillingTimeSlider — play drilling history forward
 *   BranchTree — chat fork visualizer
 *   SavedViewsList — saved-view applicator (superseded by SavedMapViewsButton)
 */

export function SectionAzimuthScrubber({ onChange }: { onChange?: (az: number) => void }) {
    const [az, setAz] = useState(90);
    return (
        <div className="p-4 rounded-md border" style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)' }}>
            <div className="flex items-center gap-2 mb-2">
                <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>Section azimuth</span>
                <span className="ml-auto text-sm font-mono" style={{ color: 'var(--accent)' }}>{az}°</span>
            </div>
            <input
                type="range"
                min={0}
                max={180}
                value={az}
                onChange={(e) => { const v = Number(e.target.value); setAz(v); onChange?.(v); }}
                className="w-full"
                style={{ accentColor: 'oklch(0.82 0.15 160)' }}
            />
            <svg width="80" height="80" viewBox="-50 -50 100 100" className="mx-auto mt-2">
                <circle cx="0" cy="0" r="40" fill="none" stroke="var(--line-2)" strokeWidth="1" />
                <line x1="0" y1="0" x2={40 * Math.sin((az * Math.PI) / 180)} y2={-40 * Math.cos((az * Math.PI) / 180)} stroke="var(--accent)" strokeWidth="2" />
                <text x="0" y="-44" fontSize="9" textAnchor="middle" fill="var(--fg-3)" fontFamily="var(--font-mono)">N</text>
            </svg>
        </div>
    );
}

export function StereonetBrush({ measurements = [] }: { measurements?: Array<{ dip_direction: number; dip: number }> }) {
    const [selected, setSelected] = useState<Set<number>>(new Set());
    const r = 90;
    return (
        <div className="p-4 rounded-md border" style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)' }}>
            <div className="flex items-center gap-2 mb-2">
                <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>Stereonet brush</span>
                <span className="ml-auto text-[10px] font-mono" style={{ color: 'var(--accent)' }}>{selected.size} selected</span>
            </div>
            <svg width="200" height="200" viewBox="-100 -100 200 200" className="mx-auto">
                <circle cx="0" cy="0" r={r} fill="none" stroke="var(--line-2)" />
                {measurements.map((m, i) => {
                    const dipRad = (m.dip * Math.PI) / 180;
                    const dirRad = (m.dip_direction * Math.PI) / 180;
                    const rho = r * Math.sin((Math.PI / 2 - dipRad) / 2) * Math.SQRT2;
                    const x = rho * Math.sin(dirRad);
                    const y = -rho * Math.cos(dirRad);
                    return (
                        <circle
                            key={i}
                            cx={x}
                            cy={y}
                            r={3}
                            fill={selected.has(i) ? 'var(--warn)' : 'var(--accent)'}
                            opacity={0.85}
                            onClick={() => setSelected((s) => { const ns = new Set(s); ns.has(i) ? ns.delete(i) : ns.add(i); return ns; })}
                            style={{ cursor: 'pointer' }}
                        />
                    );
                })}
            </svg>
            <div className="text-[10px] font-mono uppercase tracking-wider mt-1 text-center" style={{ color: 'var(--fg-3)' }}>
                click poles to brush; selected poles highlight on the map
            </div>
        </div>
    );
}

export function DrillingTimeSlider({ holes = [] }: { holes?: Array<{ hole_id: string; completed_at: string | null }> }) {
    const completed = holes.filter((h) => h.completed_at).sort((a, b) => (a.completed_at ?? '').localeCompare(b.completed_at ?? ''));
    const [idx, setIdx] = useState(completed.length);
    const [playing, setPlaying] = useState(false);
    const timer = useRef<ReturnType<typeof setInterval> | null>(null);

    function toggle() {
        if (playing) {
            timer.current && clearInterval(timer.current);
            setPlaying(false);
        } else {
            setPlaying(true);
            timer.current = setInterval(() => {
                setIdx((i) => {
                    if (i >= completed.length) { timer.current && clearInterval(timer.current); setPlaying(false); return i; }
                    return i + 1;
                });
            }, 600);
        }
    }

    return (
        <div className="p-4 rounded-md border" style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)' }}>
            <div className="flex items-center gap-2 mb-2">
                <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>Drilling history</span>
                <span className="ml-auto text-[10px] font-mono" style={{ color: 'var(--accent)' }}>{idx} / {completed.length} drilled</span>
            </div>
            <input type="range" min={0} max={completed.length} value={idx} onChange={(e) => setIdx(Number(e.target.value))} className="w-full" style={{ accentColor: 'oklch(0.82 0.15 160)' }} />
            <div className="flex items-center gap-2 mt-2">
                <button type="button" onClick={toggle} className="text-[10px] font-mono uppercase tracking-wider px-3 py-1 rounded border" style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}>
                    {playing ? '⏸ Pause' : '▶ Play'}
                </button>
                <span className="text-[10px] font-mono" style={{ color: 'var(--fg-3)' }}>
                    {completed[idx - 1]?.hole_id ?? '—'} · {completed[idx - 1]?.completed_at?.slice(0, 10) ?? ''}
                </span>
            </div>
        </div>
    );
}

export function BranchTree({ branches = [] }: { branches?: Array<{ id: string; label: string; depth: number; confidence: number }> }) {
    return (
        <div className="p-4 rounded-md border" style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)' }}>
            <div className="text-[10px] font-mono uppercase tracking-wider mb-3" style={{ color: 'var(--fg-3)' }}>Branch tree</div>
            {branches.length === 0 ? (
                <div className="text-[11px]" style={{ color: 'var(--fg-3)' }}>No forks. Type <span className="font-mono" style={{ color: 'var(--accent)' }}>/branch</span> in chat to fork.</div>
            ) : (
                <ol className="space-y-2">
                    {branches.map((b) => (
                        <li key={b.id} className="flex items-center gap-3 text-xs" style={{ paddingLeft: `${b.depth * 12}px` }}>
                            <span className="font-mono text-[10px]" style={{ color: 'var(--fg-3)' }}>{'│'.repeat(b.depth)}└─</span>
                            <span className="flex-1" style={{ color: 'var(--fg-1)' }}>{b.label}</span>
                            <span className="font-mono text-[10px]" style={{ color: 'var(--accent)' }}>{b.confidence.toFixed(2)}</span>
                        </li>
                    ))}
                </ol>
            )}
        </div>
    );
}
