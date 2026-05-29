import { Link } from '@inertiajs/react';
import { ProgressBar } from './primitives';

/**
 * OnboardingPanel — first-project empty-state walkthrough.
 * 4 steps: Connect source → Ask a hypothesis → Inspect evidence chain → Draft a report.
 */

interface OnboardingPanelProps {
    project_slug: string;
    progress?: { sources: number; investigation: boolean; report: boolean };
    onDismiss?: () => void;
}

export default function OnboardingPanel({ project_slug, progress = { sources: 0, investigation: false, report: false }, onDismiss }: OnboardingPanelProps) {
    const steps = [
        {
            id: 'sources',
            label: 'Connect your first data source',
            sub: 'Upload drill logs, connect WSGS / SEDAR+, or ingest the Wyoming Uranium archive.',
            cta: 'Open Data Import',
            href: `/projects/${project_slug}/imports/quality`,
            done: progress.sources > 0,
            count: progress.sources,
        },
        {
            id: 'investigation',
            label: 'Ask your first hypothesis',
            sub: 'Chat is the main surface. Pin sources, rank candidates, save runs.',
            cta: 'Open Chat',
            href: '/threads',
            done: progress.investigation,
        },
        {
            id: 'graph',
            label: 'Inspect the evidence chain',
            sub: 'See how conclusions trace back through facts, sources, and raw imports.',
            cta: 'Open Source Graph',
            href: `/projects/${project_slug}/graph`,
            done: false,
        },
        {
            id: 'report',
            label: 'Draft a recommendation report',
            sub: 'Every paragraph keeps its citations. Export as PDF/PPTX when ready.',
            cta: 'Open Reports',
            href: `/projects/${project_slug}/reports`,
            done: progress.report,
        },
    ];
    const doneCount = steps.filter((s) => s.done).length;
    const pct = Math.round((doneCount / steps.length) * 100);

    return (
        <div className="rounded-md border p-5" style={{ background: 'var(--bg-1)', borderColor: 'var(--accent-dim)' }}>
            <div className="flex items-center mb-4">
                <div>
                    <div className="text-[10px] font-mono uppercase tracking-[0.14em]" style={{ color: 'var(--accent)' }}>Welcome to GeoRAG</div>
                    <div className="text-sm font-medium mt-0.5" style={{ color: 'var(--fg-0)' }}>Get started in 4 steps</div>
                </div>
                <div className="ml-auto text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-2)' }}>
                    {doneCount}/{steps.length} · {pct}%
                </div>
                {onDismiss && (
                    <button type="button" onClick={onDismiss} className="ml-3 text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border" style={{ color: 'var(--fg-3)', borderColor: 'var(--line-2)' }}>
                        Dismiss
                    </button>
                )}
            </div>
            <ProgressBar value={pct} tone="accent" height={4} />
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-4">
                {steps.map((s, i) => (
                    <div key={s.id} className="p-3 rounded border" style={{ background: 'var(--bg-2)', borderColor: 'var(--line-1)' }}>
                        <div className="flex items-center gap-2 mb-1">
                            <span className="w-5 h-5 rounded-full text-[10px] font-mono flex items-center justify-center" style={{ background: s.done ? 'var(--accent)' : 'var(--bg-3)', color: s.done ? 'var(--bg-0)' : 'var(--fg-3)' }}>
                                {s.done ? '✓' : i + 1}
                            </span>
                            <span className="text-xs font-medium" style={{ color: 'var(--fg-0)' }}>{s.label}</span>
                        </div>
                        <div className="text-[11px] ml-7" style={{ color: 'var(--fg-2)' }}>{s.sub}</div>
                        <Link
                            href={s.href}
                            className="ml-7 mt-2 inline-block text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border"
                            style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                        >
                            {s.cta} →
                        </Link>
                    </div>
                ))}
            </div>
        </div>
    );
}
