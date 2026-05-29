import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, EmptyState } from '@/Components/Foundry/primitives';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';
import type { WhatChangedFeedProps, WhatChangedEvent } from '@/Types/Foundry';

const KIND_TONE: Record<string, 'accent' | 'info' | 'warn' | 'danger' | 'neutral'> = {
    evidence_new: 'accent',
    ingestion: 'info',
    hypothesis_flip: 'warn',
    retrieval_drift: 'warn',
    threshold_breach: 'danger',
    source_promoted: 'accent',
    ontology: 'info',
    decision_logged: 'neutral',
};

const IMPACT_TO_HREF = (slug: string, key: string): string => {
    switch (key) {
        case 'reasoning': return `/admin/hypothesis-workspace`;
        case 'targets': return `/projects/${slug}/targets`;
        case 'chat': return `/chat`;
        case 'audit': return `/projects/${slug}/audit`;
        case 'data': return `/admin/cluster-ingest`;
        case 'review': return `/admin/alerts-inbox`;
        case 'support': return `/admin/support-cockpit`;
        default: return `/projects/${slug}`;
    }
};

export default function FoundryWhatChangedFeed({ project, events, empty }: WhatChangedFeedProps) {
    // Phase 5 real-time push — what_changed_detector and what_changed_weekly
    // are workspace-global (no per-project broadcast), but every ingest
    // completion's `what_changed` affected_type (Phase 5 superset extension)
    // signals that new events may be available for this project.
    useWorkspaceDataUpdated(project.project_id, (event) => {
        if (event.affected_types.includes('what_changed')) {
            router.reload({ only: ['events', 'empty'] });
        }
    });

    const groups: Record<string, WhatChangedEvent[]> = events.reduce((acc, e) => {
        (acc[e.group] = acc[e.group] || []).push(e);
        return acc;
    }, {} as Record<string, WhatChangedEvent[]>);
    const orderedGroups = ['today', 'yesterday', 'this week', 'older'];

    return (
        <AppLayout>
            <Head title={`What changed · ${project.project_name}`} />

            <div className="flex-1 overflow-y-auto" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · §9.13`}
                    title="What changed since your last visit"
                    sub={`${events.length} events in the last 7 days`}
                />

                {empty ? (
                    <div className="px-8 py-12">
                        <EmptyState
                            title="Nothing has changed in this project's evidence base."
                            detail="The What Changed Detector scans new ingestions, hypothesis flips, retrieval drift, threshold breaches, and source promotions. When activity resumes, events will appear here grouped by recency."
                        />
                    </div>
                ) : (
                    <section className="px-8 py-6 max-w-3xl">
                        {orderedGroups.map((g) => {
                            const items = groups[g];
                            if (!items || items.length === 0) return null;
                            return (
                                <div key={g} className="mb-6">
                                    <div className="text-[10px] font-mono uppercase tracking-[0.14em] mb-2" style={{ color: 'var(--fg-3)' }}>
                                        {g}
                                    </div>
                                    <div className="flex flex-col gap-2">
                                        {items.map((e) => (
                                            <Card key={e.id} padded={false} className="overflow-hidden">
                                                <div className="px-4 py-3 grid grid-cols-[100px_1fr] gap-3">
                                                    <div className="flex flex-col gap-1.5">
                                                        <Pill tone={KIND_TONE[e.kind] ?? 'neutral'} dot>{e.kind.replace('_', ' ')}</Pill>
                                                        <Pill tone={e.priority === 'high' ? 'warn' : e.priority === 'med' ? 'info' : 'neutral'}>{e.priority}</Pill>
                                                    </div>
                                                    <div>
                                                        <div className="text-sm font-medium" style={{ color: 'var(--fg-0)' }}>{e.title}</div>
                                                        <div className="text-xs mt-1" style={{ color: 'var(--fg-2)' }}>{e.detail}</div>
                                                        {e.impacted.length > 0 && (
                                                            <div className="flex flex-wrap gap-1.5 mt-2">
                                                                {e.impacted.map((k) => (
                                                                    <Link
                                                                        key={k}
                                                                        href={IMPACT_TO_HREF(project.slug, k)}
                                                                        className="text-[10px] font-mono uppercase tracking-wider px-2 py-0.5 rounded border"
                                                                        style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                                                                    >
                                                                        → {k}
                                                                    </Link>
                                                                ))}
                                                            </div>
                                                        )}
                                                    </div>
                                                </div>
                                            </Card>
                                        ))}
                                    </div>
                                </div>
                            );
                        })}
                    </section>
                )}
            </div>
        </AppLayout>
    );
}
