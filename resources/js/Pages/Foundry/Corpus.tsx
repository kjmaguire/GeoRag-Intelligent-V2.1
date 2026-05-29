import { useState } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, EmptyState } from '@/Components/Foundry/primitives';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';

interface ReportRow {
    id: string;
    title: string;
    company: string;
    filing_date: string;
    version: number;
    is_scanned: boolean;
    sections_count: number;
    has_content: boolean;
    updated_at: string;
}

interface PassageRow {
    id: string;
    text: string;
    ordinal: number;
    page_first: number | null;
    page_last: number | null;
    chunk_kind: string;
    report_id: string;
    report_title: string;
}

interface EntityBucket {
    kind: string;
    count: number;
}

interface CorpusStats {
    reports: number;
    reports_with_content: number;
    passages: number;
    entity_links: number;
}

interface CorpusProps {
    project: { project_id: string; project_name: string; slug: string };
    stats: CorpusStats;
    reports: ReportRow[];
    passages: PassageRow[];
    entity_summary: EntityBucket[];
    empty: boolean;
}

type Tab = 'reports' | 'passages' | 'entities';

const TABS: Array<{ id: Tab; label: string }> = [
    { id: 'reports', label: 'Reports' },
    { id: 'passages', label: 'Indexed passages' },
    { id: 'entities', label: 'Entity links' },
];

function shortDate(s: string | null | undefined): string {
    if (!s) return '—';
    return s.slice(0, 10);
}

export default function FoundryCorpus({
    project,
    stats,
    reports,
    passages,
    entity_summary,
    empty,
}: CorpusProps) {
    const initial: Tab = reports.length > 0 ? 'reports' : passages.length > 0 ? 'passages' : 'entities';
    const [tab, setTab] = useState<Tab>(initial);

    // Phase 3 real-time push — ingest_pdf writes new silver.reports +
    // silver.passages rows; embed_pending_passages backfills embeddings.
    // 'reports' affected_type fires after every ingest completion (and after
    // embeddings catch up); the stats counts + reports list + recent passages
    // all change in lockstep.
    useWorkspaceDataUpdated(project.project_id, (event) => {
        if (event.affected_types.includes('reports')) {
            router.reload({ only: ['stats', 'reports', 'passages', 'entity_summary', 'empty'] });
        }
    });

    return (
        <AppLayout>
            <Head title={`Reader · ${project.project_name}`} />

            <div
                className="flex-1 overflow-y-auto"
                style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}
            >
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · READER`}
                    title="Documents, passages, and entity provenance"
                    sub={`${stats.reports.toLocaleString()} reports (${stats.reports_with_content.toLocaleString()} with content) · ${stats.passages.toLocaleString()} indexed passages`}
                    actions={
                        <Link
                            href={`/projects/${project.slug}/sources`}
                            className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                            style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                        >
                            Data inventory →
                        </Link>
                    }
                />

                {empty ? (
                    <div className="px-8 py-12">
                        <EmptyState
                            title="No documents in this project yet."
                            detail="Drop a PDF or XLSX filing into the Import Wizard. Once ingested, reports and indexed passages surface here."
                        />
                    </div>
                ) : (
                    <>
                        {/* Stat strip */}
                        <section className="px-8 py-4">
                            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                                <StatTile
                                    label="Reports"
                                    value={stats.reports.toLocaleString()}
                                    sub="silver.reports"
                                    tone="accent"
                                />
                                <StatTile
                                    label="With content"
                                    value={stats.reports_with_content.toLocaleString()}
                                    sub={`${stats.reports - stats.reports_with_content} metadata only`}
                                />
                                <StatTile
                                    label="Passages"
                                    value={stats.passages.toLocaleString()}
                                    sub="silver.document_passages"
                                />
                                <StatTile
                                    label="Entity links"
                                    value={stats.entity_links.toLocaleString()}
                                    sub="silver.document_entity_links"
                                />
                            </div>
                        </section>

                        {/* Tabs */}
                        <section className="px-8">
                            <div
                                className="flex items-center gap-1 border-b"
                                style={{ borderColor: 'var(--line-1)' }}
                            >
                                {TABS.map((t) => {
                                    const count =
                                        t.id === 'reports'
                                            ? reports.length
                                            : t.id === 'passages'
                                              ? passages.length
                                              : entity_summary.length;
                                    return (
                                        <button
                                            key={t.id}
                                            type="button"
                                            onClick={() => setTab(t.id)}
                                            className="px-3 py-2 text-[11px] font-mono uppercase tracking-wider transition-colors"
                                            style={{
                                                color: tab === t.id ? 'var(--accent)' : 'var(--fg-2)',
                                                borderBottom:
                                                    '2px solid ' +
                                                    (tab === t.id ? 'var(--accent)' : 'transparent'),
                                            }}
                                        >
                                            {t.label}
                                            <span style={{ color: 'var(--fg-3)', marginLeft: 6 }}>
                                                {count}
                                            </span>
                                        </button>
                                    );
                                })}
                            </div>
                        </section>

                        <section className="px-8 py-6">
                            {tab === 'reports' && <ReportsTab reports={reports} project={project} />}
                            {tab === 'passages' && <PassagesTab passages={passages} project={project} />}
                            {tab === 'entities' && <EntitiesTab entities={entity_summary} />}
                        </section>
                    </>
                )}
            </div>
        </AppLayout>
    );
}

// ── Tab: Reports ──────────────────────────────────────────────────────
function ReportsTab({ reports, project }: { reports: ReportRow[]; project: CorpusProps['project'] }) {
    if (reports.length === 0) {
        return (
            <EmptyState
                title="No reports in this project yet."
                detail="Reports land here as filings are ingested into silver.reports."
            />
        );
    }
    return (
        <Card padded={false}>
            <div
                className="grid grid-cols-[1fr_160px_110px_110px_90px_120px] text-[10px] font-mono uppercase tracking-wider px-4 py-2 border-b"
                style={{ color: 'var(--fg-3)', borderColor: 'var(--line-1)' }}
            >
                <div>Title</div>
                <div>Company</div>
                <div>Filing</div>
                <div className="text-right">Sections</div>
                <div className="text-right">v</div>
                <div>Updated</div>
            </div>
            {reports.map((r) => (
                <Link
                    key={r.id}
                    href={`/projects/${project.slug}/reports/${r.id}`}
                    className="grid grid-cols-[1fr_160px_110px_110px_90px_120px] gap-2 items-center px-4 py-2 border-b text-xs hover:bg-[var(--bg-1)] transition-colors"
                    style={{ borderColor: 'var(--line-1)' }}
                >
                    <div className="flex items-center gap-2 min-w-0">
                        <span className="truncate" style={{ color: 'var(--fg-0)' }}>
                            {r.title}
                        </span>
                        {r.is_scanned && <Pill tone="warn">scanned</Pill>}
                        {!r.has_content && <Pill tone="neutral">meta only</Pill>}
                    </div>
                    <div className="font-mono text-[11px] truncate" style={{ color: 'var(--fg-2)' }}>
                        {r.company || '—'}
                    </div>
                    <div className="font-mono text-[11px]" style={{ color: 'var(--fg-2)' }}>
                        {shortDate(r.filing_date)}
                    </div>
                    <div
                        className="font-mono text-right"
                        style={{ color: r.has_content ? 'var(--accent)' : 'var(--fg-3)' }}
                    >
                        {r.sections_count}
                    </div>
                    <div className="font-mono text-right" style={{ color: 'var(--fg-2)' }}>
                        v{r.version}
                    </div>
                    <div className="font-mono text-[11px]" style={{ color: 'var(--fg-3)' }}>
                        {shortDate(r.updated_at)}
                    </div>
                </Link>
            ))}
        </Card>
    );
}

// ── Tab: Indexed passages ─────────────────────────────────────────────
function PassagesTab({ passages, project }: { passages: PassageRow[]; project: CorpusProps['project'] }) {
    if (passages.length === 0) {
        return (
            <EmptyState
                title="No indexed passages for this project."
                detail="The §04p PDF stack chunks reports into passages. If reports are populated but passages are 0, ingest may not have completed — check IngestQuality."
            />
        );
    }
    return (
        <Card eyebrow="SILVER · DOCUMENT_PASSAGES" title={`${passages.length} chunked passages (first 30)`}>
            <div className="space-y-2">
                {passages.map((p) => (
                    <div
                        key={p.id}
                        className="p-3 rounded border"
                        style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)' }}
                    >
                        <div
                            className="flex items-center gap-2 mb-2 text-[10px] font-mono uppercase tracking-wider"
                            style={{ color: 'var(--fg-3)' }}
                        >
                            <Pill tone="neutral">ord {p.ordinal}</Pill>
                            {p.page_first !== null && (
                                <Pill tone="info">
                                    p.{p.page_first}
                                    {p.page_last && p.page_last !== p.page_first ? `-${p.page_last}` : ''}
                                </Pill>
                            )}
                            {p.chunk_kind && <Pill tone="neutral">{p.chunk_kind}</Pill>}
                            {p.report_id && (
                                <Link
                                    href={`/projects/${project.slug}/reports/${p.report_id}`}
                                    className="ml-auto truncate hover:underline"
                                    style={{ color: 'var(--accent)' }}
                                >
                                    {p.report_title || p.report_id.slice(0, 8)} →
                                </Link>
                            )}
                        </div>
                        <div
                            className="text-[12px] whitespace-pre-wrap leading-relaxed"
                            style={{ color: 'var(--fg-1)' }}
                        >
                            {p.text}
                        </div>
                    </div>
                ))}
            </div>
        </Card>
    );
}

// ── Tab: Entities ─────────────────────────────────────────────────────
function EntitiesTab({ entities }: { entities: EntityBucket[] }) {
    if (entities.length === 0) {
        return (
            <EmptyState
                title="No entity links for this project."
                detail="Entity resolution (§04i Layer 4) writes rows to silver.document_entity_links as the ontology recognises geological entities in passages. If reports/passages are populated but this is empty, the ontology pipeline hasn't run yet."
            />
        );
    }
    const total = entities.reduce((a, e) => a + e.count, 0);
    return (
        <Card eyebrow="SILVER · DOCUMENT_ENTITY_LINKS" title={`${total.toLocaleString()} entity links across ${entities.length} kinds`}>
            <div className="space-y-1">
                {entities.map((e) => {
                    const pct = total > 0 ? (e.count / total) * 100 : 0;
                    return (
                        <div
                            key={e.kind}
                            className="grid grid-cols-[160px_1fr_80px] gap-3 items-center px-3 py-2 border-b text-xs"
                            style={{ borderColor: 'var(--line-1)' }}
                        >
                            <div>
                                <Pill tone="info">{e.kind}</Pill>
                            </div>
                            <div className="relative h-3 rounded overflow-hidden" style={{ background: 'var(--bg-2)' }}>
                                <div
                                    className="absolute inset-y-0 left-0"
                                    style={{
                                        width: `${Math.max(2, pct)}%`,
                                        background: 'var(--accent)',
                                        opacity: 0.7,
                                    }}
                                />
                            </div>
                            <div className="font-mono text-right" style={{ color: 'var(--fg-1)' }}>
                                {e.count.toLocaleString()}
                            </div>
                        </div>
                    );
                })}
            </div>
        </Card>
    );
}

// ── Helpers ───────────────────────────────────────────────────────────
function StatTile({
    label,
    value,
    sub,
    tone,
}: {
    label: string;
    value: string;
    sub?: string;
    tone?: 'accent' | 'info' | 'warn' | 'neutral';
}) {
    const color =
        tone === 'accent'
            ? 'var(--accent)'
            : tone === 'warn'
              ? 'var(--warn, #d6a14a)'
              : tone === 'info'
                ? 'var(--info, #6aa7ff)'
                : 'var(--fg-0)';
    return (
        <div
            className="p-3 rounded-md border"
            style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)' }}
        >
            <div
                className="text-[10px] font-mono uppercase tracking-wider mb-1"
                style={{ color: 'var(--fg-3)' }}
            >
                {label}
            </div>
            <div className="text-xl font-mono" style={{ color }}>
                {value}
            </div>
            {sub && (
                <div className="text-[10px] font-mono mt-1" style={{ color: 'var(--fg-3)' }}>
                    {sub}
                </div>
            )}
        </div>
    );
}
