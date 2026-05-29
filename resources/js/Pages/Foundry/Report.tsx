import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, EmptyState } from '@/Components/Foundry/primitives';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';

interface Report {
    report_id: string;
    title: string;
    company: string;
    filing_date: string;
    commodity: string;
    parse_quality_pct: number | null;
    version: number;
    is_scanned: boolean;
    sections_count: number;
    has_content: boolean;
}

interface ReportProps {
    project: { project_id: string; project_name: string; slug: string };
    reports: Report[];
    is_admin: boolean;
    empty: boolean;
}

export default function FoundryReport({ project, reports, is_admin, empty }: ReportProps) {
    // Phase 3 real-time push — ingest_pdf and generate_report both write
    // new silver.reports rows; the parse_quality_pct + sections_count fields
    // also change as the pipeline progresses. Refresh on 'reports' affected_type.
    useWorkspaceDataUpdated(project.project_id, (event) => {
        if (event.affected_types.includes('reports')) {
            router.reload({ only: ['reports', 'empty'] });
        }
    });

    return (
        <AppLayout>
            <Head title={`Reports · ${project.project_name}`} />

            <div
                className="flex-1 overflow-y-auto"
                style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}
            >
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · REPORTS`}
                    title="Ingested filings & NI 43-101 drafts"
                    sub={`${reports.length} report${reports.length === 1 ? '' : 's'} linked to this project`}
                    actions={
                        is_admin ? (
                            <Link
                                href="/admin/reports"
                                className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                style={{
                                    color: 'var(--accent)',
                                    background: 'var(--accent-bg)',
                                    borderColor: 'var(--accent-dim)',
                                }}
                            >
                                + New draft (admin)
                            </Link>
                        ) : undefined
                    }
                />

                {empty ? (
                    <div className="px-8 py-12">
                        <EmptyState
                            title="No reports linked to this project yet."
                            detail="Drop a PDF or XLSX filing into the Import Wizard — once ingested, it lands in silver.reports and surfaces here. Admins can also draft a new NI 43-101 from scratch in the admin builder."
                        />
                    </div>
                ) : (
                    <section className="px-8 py-6 grid grid-cols-1 md:grid-cols-2 gap-3">
                        {reports.map((r) => (
                            <Card
                                key={r.report_id}
                                eyebrow={
                                    <span className="flex items-center gap-2">
                                        <Pill tone="info">v{r.version}</Pill>
                                        {r.is_scanned && <Pill tone="warn">scanned</Pill>}
                                        {typeof r.parse_quality_pct === 'number' && (
                                            <Pill tone={r.parse_quality_pct >= 90 ? 'accent' : 'warn'}>
                                                parse {r.parse_quality_pct.toFixed(0)}%
                                            </Pill>
                                        )}
                                        {!r.has_content && <Pill tone="neutral">metadata only</Pill>}
                                    </span>
                                }
                                title={r.title}
                            >
                                <div
                                    className="grid grid-cols-2 gap-2 text-[11px]"
                                    style={{ color: 'var(--fg-2)' }}
                                >
                                    <div>
                                        COMPANY{' '}
                                        <span style={{ color: 'var(--fg-0)' }}>{r.company || '—'}</span>
                                    </div>
                                    <div>
                                        FILING{' '}
                                        <span style={{ color: 'var(--fg-0)' }}>
                                            {r.filing_date ? r.filing_date.slice(0, 10) : '—'}
                                        </span>
                                    </div>
                                    <div>
                                        COMMODITY{' '}
                                        <span style={{ color: 'var(--fg-0)' }}>{r.commodity || '—'}</span>
                                    </div>
                                    <div>
                                        SECTIONS{' '}
                                        <span style={{ color: 'var(--fg-0)' }}>{r.sections_count}</span>
                                    </div>
                                </div>
                                <Link
                                    href={`/projects/${project.slug}/reports/${r.report_id}`}
                                    className="inline-block mt-3 text-[10px] font-mono uppercase tracking-wider px-2.5 py-1 rounded border"
                                    style={{
                                        color: r.has_content ? 'var(--accent)' : 'var(--fg-2)',
                                        background: r.has_content ? 'var(--accent-bg)' : 'transparent',
                                        borderColor: r.has_content ? 'var(--accent-dim)' : 'var(--line-2)',
                                    }}
                                >
                                    {r.has_content ? 'Open report →' : 'View metadata →'}
                                </Link>
                            </Card>
                        ))}
                    </section>
                )}
            </div>
        </AppLayout>
    );
}
