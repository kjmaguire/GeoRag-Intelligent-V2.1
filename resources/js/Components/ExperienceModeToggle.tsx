import type { JSX } from 'react';
import { useEffect, useState } from 'react';

/**
 * §19.5 Experience-mode toggle.
 *
 * 4 modes per master plan: Geologist (default) / Executive / GIS / Admin.
 * The mode is persisted in localStorage + broadcast via CustomEvent so
 * any listening component can re-render. v1 uses it primarily to
 * highlight different navigation surfaces; v2 will branch deeper UI
 * affordances per mode (e.g. Executive sees plain-English summaries
 * by default, GIS sees the map first).
 */

export type ExperienceMode = 'geologist' | 'executive' | 'gis' | 'admin';

export const EXPERIENCE_MODES: { id: ExperienceMode; label: string; icon: string; description: string }[] = [
    { id: 'geologist', label: 'Geologist', icon: '⛰', description: 'Default — chat + maps + interpretation workspace' },
    { id: 'executive', label: 'Executive', icon: '📊', description: 'Dashboards-first, plain-English summaries' },
    { id: 'gis',       label: 'GIS',       icon: '🗺',  description: 'Map-first, layer controls, less chat' },
    { id: 'admin',     label: 'Admin',     icon: '⚙', description: 'Admin cockpit-first, operator surfaces' },
];

const LS_KEY = 'georag_experience_mode';
const EVENT_NAME = 'georag:experience-mode-change';

export function getExperienceMode(): ExperienceMode {
    if (typeof window === 'undefined') return 'geologist';
    const m = window.localStorage.getItem(LS_KEY);
    if (m === 'executive' || m === 'gis' || m === 'admin') return m;
    return 'geologist';
}

export function setExperienceMode(mode: ExperienceMode): void {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem(LS_KEY, mode);
    window.dispatchEvent(new CustomEvent(EVENT_NAME, { detail: mode }));
}

/** Hook — returns the current mode + re-renders on change. */
export function useExperienceMode(): [ExperienceMode, (m: ExperienceMode) => void] {
    const [mode, setMode] = useState<ExperienceMode>(getExperienceMode);

    useEffect(() => {
        function onChange(e: Event) {
            const ce = e as CustomEvent<ExperienceMode>;
            if (ce.detail) setMode(ce.detail);
        }
        window.addEventListener(EVENT_NAME, onChange);
        return () => window.removeEventListener(EVENT_NAME, onChange);
    }, []);

    return [mode, (m) => { setExperienceMode(m); }];
}

export function ExperienceModeToggle({ isAdmin }: { isAdmin: boolean }): JSX.Element {
    const [mode, setMode] = useExperienceMode();
    const [open, setOpen] = useState(false);
    const current = EXPERIENCE_MODES.find((m) => m.id === mode) ?? EXPERIENCE_MODES[0];

    const available = EXPERIENCE_MODES.filter((m) => isAdmin || m.id !== 'admin');

    return (
        <div className="relative">
            <button
                type="button"
                onClick={() => setOpen((v) => !v)}
                onBlur={(e) => {
                    if (!e.currentTarget.parentElement?.contains(e.relatedTarget as Node)) setOpen(false);
                }}
                className="flex items-center gap-1.5 rounded border border-gray-800 bg-gray-900 px-2 py-1 text-xs font-medium text-gray-300 hover:bg-gray-800"
                aria-haspopup="true"
                aria-expanded={open}
                title={current.description}
            >
                <span aria-hidden="true">{current.icon}</span>
                <span>{current.label}</span>
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="h-3 w-3">
                    <path fillRule="evenodd" d="M5.23 7.21a.75.75 0 0 1 1.06.02L10 11.06l3.71-3.83a.75.75 0 1 1 1.08 1.04l-4.25 4.39a.75.75 0 0 1-1.08 0L5.21 8.27a.75.75 0 0 1 .02-1.06Z" clipRule="evenodd" />
                </svg>
            </button>

            {open && (
                <div className="absolute right-0 top-full mt-1 w-64 rounded border border-gray-800 bg-gray-900 shadow-lg z-50">
                    <div className="px-3 py-2 border-b border-gray-800 text-[10px] uppercase tracking-widest text-gray-500">
                        Experience mode
                    </div>
                    {available.map((m) => (
                        <button
                            key={m.id}
                            type="button"
                            onClick={() => { setMode(m.id); setOpen(false); }}
                            className={`w-full text-left px-3 py-2 text-xs hover:bg-gray-800 ${
                                m.id === mode ? 'bg-amber-950/40 text-amber-300' : 'text-gray-200'
                            }`}
                        >
                            <div className="flex items-center gap-2">
                                <span aria-hidden="true">{m.icon}</span>
                                <span className="font-medium">{m.label}</span>
                                {m.id === mode && <span className="ml-auto text-[10px]">✓</span>}
                            </div>
                            <div className="mt-0.5 text-[10px] text-gray-500">{m.description}</div>
                        </button>
                    ))}
                </div>
            )}
        </div>
    );
}
