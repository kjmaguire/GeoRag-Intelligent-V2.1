/**
 * DataQualityFlagsBadge — Plan §6a badge UI for silver.data_quality_flags.
 *
 * Renders a compact three-dot indicator: red (ERROR), amber (WARNING),
 * grey (INFO) with the count of open flags per severity. Clicking
 * expands to the flag list with description + rule_id + flagged_at.
 *
 * Hidden entirely when there are zero flags — collars in good shape
 * shouldn't have UI noise pulling attention.
 *
 * Used on the DrillholeDetail page. The same shape works for any
 * record_type — when the badge ships on Document/Report views the
 * component is reusable without changes.
 */

import { useState } from 'react';

export interface DataQualityFlag {
    flag_id: string;
    flag_type: string;
    severity: 'ERROR' | 'WARNING' | 'INFO';
    description: string;
    rule_id?: string | null;
    rule_version?: string | null;
    flagged_at: string;
}

export interface DataQualityFlagsBadgeData {
    counts: { ERROR: number; WARNING: number; INFO: number };
    open_total: number;
    flags: DataQualityFlag[];
}

interface DataQualityFlagsBadgeProps {
    data?: DataQualityFlagsBadgeData | null;
    /** Display label shown next to the dots. Default "Quality". */
    label?: string;
}

const SEVERITY_TONE: Record<DataQualityFlag['severity'], {
    dot: string;
    text: string;
    bg: string;
    border: string;
    badge: string;
}> = {
    ERROR: {
        dot: 'bg-red-500',
        text: 'text-red-300',
        bg: 'bg-red-950/40',
        border: 'border-red-800/60',
        badge: 'bg-red-900/70 text-red-100',
    },
    WARNING: {
        dot: 'bg-amber-500',
        text: 'text-amber-300',
        bg: 'bg-amber-950/40',
        border: 'border-amber-800/60',
        badge: 'bg-amber-900/70 text-amber-100',
    },
    INFO: {
        dot: 'bg-gray-400',
        text: 'text-gray-300',
        bg: 'bg-gray-900/40',
        border: 'border-gray-700/60',
        badge: 'bg-gray-800/70 text-gray-200',
    },
};


export default function DataQualityFlagsBadge({
    data,
    label = 'Quality',
}: DataQualityFlagsBadgeProps) {
    const [expanded, setExpanded] = useState(false);

    if (!data || data.open_total === 0) {
        // No flags → no UI noise. Returning null keeps the parent
        // layout clean for the well-behaved-collar happy path.
        return null;
    }

    const { counts, flags, open_total } = data;

    return (
        <div className="inline-block relative">
            <button
                type="button"
                onClick={() => setExpanded((v) => !v)}
                className="inline-flex items-center gap-2 px-2.5 py-1 rounded-md border border-gray-700/80 bg-gray-900/80 hover:bg-gray-800/80 focus:outline-none focus:ring-2 focus:ring-amber-500/40 text-xs"
                aria-expanded={expanded}
                aria-label={`${label}: ${open_total} data-quality flag${open_total === 1 ? '' : 's'}`}
            >
                <span className="text-gray-400 font-medium uppercase tracking-wider text-[10px]">{label}</span>
                <span className="inline-flex items-center gap-1.5">
                    {(['ERROR', 'WARNING', 'INFO'] as const).map((sev) => {
                        const n = counts[sev] ?? 0;
                        if (n === 0) {
                            return null;
                        }
                        const tone = SEVERITY_TONE[sev];
                        return (
                            <span
                                key={sev}
                                className="inline-flex items-center gap-1"
                                title={`${n} ${sev.toLowerCase()}${n === 1 ? '' : 's'}`}
                            >
                                <span className={`w-1.5 h-1.5 rounded-full ${tone.dot}`} aria-hidden="true" />
                                <span className={`tabular-nums ${tone.text}`}>{n}</span>
                            </span>
                        );
                    })}
                </span>
                <svg
                    xmlns="http://www.w3.org/2000/svg"
                    viewBox="0 0 20 20"
                    fill="currentColor"
                    className={`w-3 h-3 text-gray-500 transition-transform ${expanded ? 'rotate-180' : ''}`}
                    aria-hidden="true"
                >
                    <path fillRule="evenodd" d="M5.23 7.21a.75.75 0 011.06.02L10 11.06l3.71-3.83a.75.75 0 111.08 1.04l-4.25 4.39a.75.75 0 01-1.08 0L5.21 8.27a.75.75 0 01.02-1.06z" clipRule="evenodd" />
                </svg>
            </button>

            {expanded && (
                <div className="absolute z-10 mt-2 w-96 max-w-[90vw] rounded-lg border border-gray-700/80 bg-gray-950 shadow-xl">
                    <div className="px-3 py-2 border-b border-gray-800 flex items-center justify-between">
                        <div className="text-xs text-gray-300 font-medium">
                            {open_total} open flag{open_total === 1 ? '' : 's'}
                        </div>
                        <button
                            type="button"
                            onClick={() => setExpanded(false)}
                            className="text-gray-500 hover:text-gray-200 p-1"
                            aria-label="Close flag list"
                        >
                            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" className="w-3 h-3">
                                <path fillRule="evenodd" d="M5.47 5.47a.75.75 0 0 1 1.06 0L12 10.94l5.47-5.47a.75.75 0 1 1 1.06 1.06L13.06 12l5.47 5.47a.75.75 0 1 1-1.06 1.06L12 13.06l-5.47 5.47a.75.75 0 0 1-1.06-1.06L10.94 12 5.47 6.53a.75.75 0 0 1 0-1.06Z" clipRule="evenodd" />
                            </svg>
                        </button>
                    </div>
                    <ul className="max-h-80 overflow-y-auto divide-y divide-gray-800/70">
                        {flags.map((f) => {
                            const tone = SEVERITY_TONE[f.severity];
                            return (
                                <li key={f.flag_id} className={`px-3 py-2 ${tone.bg} border-l-2 ${tone.border}`}>
                                    <div className="flex items-center gap-2 mb-1">
                                        <span className={`text-[10px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded ${tone.badge}`}>
                                            {f.severity}
                                        </span>
                                        <code className="text-[10px] text-gray-400 font-mono truncate">
                                            {f.flag_type}
                                        </code>
                                    </div>
                                    <p className="text-xs text-gray-200 leading-snug">{f.description}</p>
                                    {f.rule_version && (
                                        <div className="mt-1 text-[10px] text-gray-500 font-mono">
                                            rule {f.rule_id ?? f.flag_type} · {f.rule_version}
                                        </div>
                                    )}
                                </li>
                            );
                        })}
                    </ul>
                </div>
            )}
        </div>
    );
}
