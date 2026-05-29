import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import Stereonet from '../HoleAnalysis/Stereonet';

const Stereosphere = lazy(() => import('../HoleAnalysis/Stereosphere'));

interface Structure {
    collar_id: string;
    depth: number;
    structure_type: string;
    true_dip: number | null;
    dip_direction: number | null;
}

interface Props { structures: Structure[]; }

/**
 * Project-wide stereonet aggregating every structural measurement from
 * every hole in the project. Reuses the per-hole `Stereonet` (2-D SVG)
 * and `Stereosphere` (3-D Plotly) components for rendering — feeding
 * them the full cross-hole structure list rather than a single hole's.
 *
 * The structure-type filter controls + the 2-D/3-D toggle carry the
 * same UX as the per-hole tabs so users see a familiar instrument.
 */
export default function AggregateStereonet({ structures }: Props) {
    const [view, setView] = useState<'2d' | '3d'>('2d');
    const [visibleTypes, setVisibleTypes] = useState<Record<string, boolean>>({});

    const typeCounts = useMemo(() => {
        const counts: Record<string, number> = {};
        for (const s of structures) counts[s.structure_type] = (counts[s.structure_type] || 0) + 1;
        return counts;
    }, [structures]);

    // Initialise visibility so newly-seen types default to visible.
    // Kept in an effect — calling setState from useMemo is a React no-no.
    useEffect(() => {
        setVisibleTypes((prev) => {
            const next = { ...prev };
            let changed = false;
            for (const t of Object.keys(typeCounts)) {
                if (next[t] === undefined) { next[t] = true; changed = true; }
            }
            return changed ? next : prev;
        });
    }, [typeCounts]);

    if (structures.length === 0) {
        return <div className="h-[360px] flex items-center justify-center text-sm text-gray-500">No structural measurements in this project.</div>;
    }

    return (
        <div className="flex flex-col lg:flex-row gap-4">
            <aside className="lg:w-60 shrink-0 space-y-3">
                <div className="flex items-center justify-between">
                    <div className="text-xs uppercase tracking-wide text-gray-500">Structure types</div>
                    <div role="group" aria-label="View dimension" className="inline-flex rounded-full border border-gray-700 bg-gray-900/60 p-0.5 text-[11px] font-medium">
                        {(['2d', '3d'] as const).map((m) => {
                            const active = view === m;
                            return (
                                <button
                                    key={m}
                                    type="button"
                                    onClick={() => setView(m)}
                                    aria-pressed={active}
                                    className={`px-2.5 py-0.5 rounded-full transition-colors ${active ? 'bg-amber-400 text-gray-950' : 'text-gray-400 hover:text-gray-200'}`}
                                >
                                    {m === '2d' ? '2D' : '3D'}
                                </button>
                            );
                        })}
                    </div>
                </div>
                <div className="space-y-1.5">
                    {Object.entries(typeCounts).map(([type, n]) => (
                        <label key={type} className="flex items-center gap-2 text-xs text-gray-300 cursor-pointer select-none">
                            <input
                                type="checkbox"
                                checked={visibleTypes[type] ?? true}
                                onChange={(e) => setVisibleTypes((p) => ({ ...p, [type]: e.target.checked }))}
                                className="accent-amber-400"
                            />
                            <span className="capitalize">{type}</span>
                            <span className="text-gray-500">({n})</span>
                        </label>
                    ))}
                </div>
                <div className="text-[10px] text-gray-500 leading-relaxed pt-2 border-t border-gray-800">
                    {structures.length} measurements aggregated across every hole in the project.
                </div>
            </aside>
            <div className="flex-1">
                {view === '3d' ? (
                    <Suspense fallback={<div className="flex items-center justify-center h-full text-xs text-gray-500">Loading 3-D hemisphere…</div>}>
                        <Stereosphere structures={structures} holeId="project-wide" visibleTypes={visibleTypes} />
                    </Suspense>
                ) : (
                    <Stereonet structures={structures} holeId="project-wide" visibleTypes={visibleTypes} />
                )}
            </div>
        </div>
    );
}
