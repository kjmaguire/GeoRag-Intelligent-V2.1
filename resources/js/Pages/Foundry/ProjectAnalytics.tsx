import { Head, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Stat, EmptyState } from '@/Components/Foundry/primitives';
import { RefusalByGate, ConfidenceHistogram, InvestigationFunnel, PerJurisdictionVolume } from '@/Components/Foundry/Charts';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';

interface ProjectAnalyticsProps {
    project: { project_id: string; project_name: string; slug: string; region: string | null; commodity: string | null };
    window_days: number;
    kpis: Array<{ label: string; value: string; sub?: string; tone?: string }>;
    refusal_by_week: Array<{ week: string; gates: Record<string, number> }>;
    confidence_histogram: Array<{ low: number; high: number; count: number }>;
    top_queries: Array<{ text: string; freq: number }>;
    empty: boolean;
}

export default function FoundryProjectAnalytics({ project, window_days, kpis, refusal_by_week, confidence_histogram, top_queries, empty }: ProjectAnalyticsProps) {
    // Phase 3 real-time push — this page rolls up silver.collars +
    // audit.query_audit_log. The 'reports' and 'collars' affected types
    // cover the silver side; 'audit_log' covers new query rows. All three
    // change the KPI numbers / histograms.
    useWorkspaceDataUpdated(project.project_id, (event) => {
        const t = event.affected_types;
        if (t.includes('audit_log') || t.includes('reports') || t.includes('collars')) {
            router.reload({
                only: ['kpis', 'refusal_by_week', 'confidence_histogram', 'top_queries', 'empty'],
            });
        }
    });

    return (
        <AppLayout>
            <Head title={`Analytics · ${project.project_name}`} />

            <div className="flex-1 overflow-y-auto" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · ANALYTICS`}
                    title="RAG quality + drill economics"
                    sub={`${window_days}d window · ${project.region ?? 'no region'} · ${project.commodity ?? 'no commodity'}`}
                />

                {empty ? (
                    <div className="px-8 py-12">
                        <EmptyState
                            title="No data yet."
                            detail="This project has no collars and no audit-log activity in the window. Run an ingestion + query a few times to populate analytics."
                        />
                    </div>
                ) : (
                    <>
                        <section className="grid grid-cols-2 sm:grid-cols-5 gap-px px-8 py-5" style={{ background: 'var(--line-1)' }}>
                            {kpis.map((k, i) => (
                                <Stat key={i} label={k.label} value={k.value} sub={k.sub} tone={k.tone as 'accent' | 'warn' | 'neutral' | undefined} />
                            ))}
                        </section>

                        <section className="px-8 py-6 grid grid-cols-1 lg:grid-cols-2 gap-4">
                            <Card eyebrow="REFUSAL BY GATE · 12W" title="Hallucination guardrails">
                                <RefusalByGate weeks={refusal_by_week} />
                            </Card>
                            <Card eyebrow="CONFIDENCE CALIBRATION" title={`${confidence_histogram.reduce((a, b) => a + b.count, 0)} answered queries`}>
                                <ConfidenceHistogram bins={confidence_histogram} refusalFloor={0.5} />
                            </Card>
                            <Card eyebrow="INVESTIGATION FUNNEL" title="Created → cited">
                                <InvestigationFunnel />
                            </Card>
                            <Card eyebrow="TOP QUERIES" title={`${top_queries.length} most-frequent`}>
                                {top_queries.length === 0 ? (
                                    <div className="text-xs" style={{ color: 'var(--fg-3)' }}>No queries in window.</div>
                                ) : (
                                    <ul className="space-y-2 text-xs">
                                        {top_queries.map((q, i) => (
                                            <li key={i} className="flex justify-between gap-3 border-b py-1.5" style={{ borderColor: 'var(--line-1)' }}>
                                                <span className="truncate" style={{ color: 'var(--fg-1)' }}>{q.text}</span>
                                                <span className="font-mono" style={{ color: 'var(--accent)' }}>{q.freq}×</span>
                                            </li>
                                        ))}
                                    </ul>
                                )}
                            </Card>
                        </section>
                    </>
                )}
            </div>
        </AppLayout>
    );
}
