import { useState } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, EmptyState } from '@/Components/Foundry/primitives';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';
import DataQualityFlagsBadge, {
    type DataQualityFlagsBadgeData,
} from '@/Components/DataQualityFlagsBadge';

interface Section {
    heading: string;
    body: string;
    kind: string;
    index: number;
}

interface Passage {
    id: string;
    text: string;
    ordinal: number;
    page_first: number | null;
    page_last: number | null;
    chunk_kind: string;
}

interface ReportRow {
    report_id: string;
    title: string;
    company: string;
    filing_date: string;
    commodity: string;
    version: number;
    region: string;
    project_name: string;
    created_at: string;
    updated_at: string;
}

interface Figure {
    idx: number;
    page: number | null;
    bbox: [number, number, number, number] | null;
    caption: string;
    key: string;
    sha256: string | null;
    url: string;          // presigned MinIO URL (1-hour TTL)
    expires_at: string;
}

interface ReportViewProps {
    project: { project_id: string; project_name: string; slug: string };
    report: ReportRow;
    sections: Section[];
    passages: Passage[];
    figures?: Figure[];
    data_quality_flags?: DataQualityFlagsBadgeData | null;
    is_admin: boolean;
    empty: boolean;
}

type Tab = 'sections' | 'passages' | 'figures' | 'metadata';

const TABS: Array<{ id: Tab; label: string }> = [
    { id: 'sections', label: 'Sections' },
    { id: 'passages', label: 'Indexed passages' },
    { id: 'figures', label: 'Figures' },
    { id: 'metadata', label: 'Metadata' },
];

function shortDate(s: string | null | undefined): string {
    if (!s) return '—';
    return s.slice(0, 16).replace('T', ' ');
}

export default function FoundryReportView({
    project,
    report,
    sections,
    passages,
    figures = [],
    data_quality_flags = null,
    is_admin,
    empty,
}: ReportViewProps) {
    // Phase 5 real-time push — ingest_pdf re-runs / OCR re-runs /
    // figure re-rendering all change figures+sections+passages for the
    // open report. Filter on `reports` (covers all ingest paths).
    useWorkspaceDataUpdated(project.project_id, (event) => {
        if (event.affected_types.includes('reports')) {
            router.reload({
                only: ['figures', 'sections', 'passages', 'report', 'data_quality_flags'],
            });
        }
        // Plan §6a — re-pull flags when the DQ table changes too. The
        // daily silver_dq_daily_schedule materialise emits this event
        // type so the badge stays fresh without a hard refresh.
        if (event.affected_types.includes('data_quality_flags')) {
            router.reload({ only: ['data_quality_flags'] });
        }
    });

    const initial: Tab =
        sections.length > 0
            ? 'sections'
            : passages.length > 0
              ? 'passages'
              : figures.length > 0
                ? 'figures'
                : 'metadata';
    const [tab, setTab] = useState<Tab>(initial);

    return (
        <AppLayout>
            <Head title={`${report.title} · ${project.project_name}`} />

            <div
                className="flex-1 overflow-y-auto"
                style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}
            >
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · REPORT`}
                    title={report.title}
                    sub={[
                        report.company,
                        report.filing_date ? report.filing_date.slice(0, 10) : null,
                        report.commodity,
                        `v${report.version}`,
                    ]
                        .filter(Boolean)
                        .join(' · ')}
                    actions={
                        <div className="flex gap-2 items-center">
                            <DataQualityFlagsBadge data={data_quality_flags} />
                            <Link
                                href={`/projects/${project.slug}/reports`}
                                className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                            >
                                ← Back to reports
                            </Link>
                            {is_admin && (
                                <Link
                                    href="/admin/reports"
                                    className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                    style={{
                                        color: 'var(--accent)',
                                        background: 'var(--accent-bg)',
                                        borderColor: 'var(--accent-dim)',
                                    }}
                                >
                                    Open in admin builder →
                                </Link>
                            )}
                        </div>
                    }
                />

                {/* Tabs */}
                <section className="px-8">
                    <div
                        className="flex items-center gap-1 border-b"
                        style={{ borderColor: 'var(--line-1)' }}
                    >
                        {TABS.map((t) => {
                            const count =
                                t.id === 'sections'
                                    ? sections.length
                                    : t.id === 'passages'
                                      ? passages.length
                                      : t.id === 'figures'
                                        ? figures.length
                                        : null;
                            return (
                                <button
                                    key={t.id}
                                    type="button"
                                    onClick={() => setTab(t.id)}
                                    className="px-3 py-2 text-[11px] font-mono uppercase tracking-wider transition-colors"
                                    style={{
                                        color: tab === t.id ? 'var(--accent)' : 'var(--fg-2)',
                                        borderBottom:
                                            '2px solid ' + (tab === t.id ? 'var(--accent)' : 'transparent'),
                                    }}
                                >
                                    {t.label}
                                    {count !== null && (
                                        <span style={{ color: 'var(--fg-3)', marginLeft: 6 }}>
                                            {count}
                                        </span>
                                    )}
                                </button>
                            );
                        })}
                    </div>
                </section>

                <section className="px-8 py-6 space-y-3">
                    {tab === 'sections' && <SectionsTab sections={sections} empty={empty} />}
                    {tab === 'passages' && <PassagesTab passages={passages} />}
                    {tab === 'figures' && <FiguresTab figures={figures} />}
                    {tab === 'metadata' && <MetadataTab report={report} />}
                </section>
            </div>
        </AppLayout>
    );
}

function FiguresTab({ figures }: { figures: Figure[] }) {
    if (figures.length === 0) {
        return (
            <EmptyState
                title="No figures extracted from this report yet."
                detail={
                    "Figures are pulled when the §04p ingest pipeline runs with " +
                    "PDF_PARSER_DOCLING_ENABLED=true. Once it has, each figure's " +
                    "PNG + caption + page reference will appear here with a 1-hour " +
                    "presigned download URL."
                }
            />
        );
    }

    return (
        <div
            className="grid gap-4"
            style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))' }}
        >
            {figures.map((f) => (
                <figure
                    key={f.key}
                    className="rounded border overflow-hidden"
                    style={{ borderColor: 'var(--line-2)', background: 'var(--bg-1)' }}
                >
                    <a
                        href={f.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        title={`Open full size · ${f.key}`}
                    >
                        <img
                            src={f.url}
                            alt={f.caption || `Figure ${f.idx + 1}`}
                            loading="lazy"
                            style={{
                                display: 'block',
                                width: '100%',
                                height: 'auto',
                                maxHeight: 280,
                                objectFit: 'contain',
                                background: 'var(--bg-0)',
                            }}
                        />
                    </a>
                    <figcaption
                        className="px-3 py-2 text-[11px] font-mono"
                        style={{ color: 'var(--fg-2)', borderTop: '1px solid var(--line-1)' }}
                    >
                        <div style={{ color: 'var(--fg-1)' }}>
                            {f.caption || <em style={{ color: 'var(--fg-3)' }}>no caption</em>}
                        </div>
                        <div
                            className="mt-1 flex items-center justify-between"
                            style={{ color: 'var(--fg-3)' }}
                        >
                            <span>FIG {String(f.idx + 1).padStart(3, '0')}</span>
                            <span>{f.page !== null ? `p. ${f.page}` : ''}</span>
                        </div>
                    </figcaption>
                </figure>
            ))}
        </div>
    );
}

function SectionsTab({ sections, empty }: { sections: Section[]; empty: boolean }) {
    if (sections.length === 0) {
        return (
            <EmptyState
                title="No sections_text on this report yet."
                detail={
                    empty
                        ? "This silver.reports row exists as metadata only — the source filing was either not parsed yet, scanned-image (Tier 2 OCR pending), or the §04p PDF stack hasn't run on it. Once it runs, structured sections + chunked passages will populate here."
                        : "Sections are empty but indexed passages exist — switch to the Passages tab."
                }
            />
        );
    }
    return (
        <>
            {sections.map((s) => (
                <Card
                    key={s.index}
                    eyebrow={`§ ${s.index + 1}${s.kind && s.kind !== 'para' ? ' · ' + s.kind : ''}`}
                    title={s.heading || 'Untitled section'}
                >
                    <div
                        className="text-[13px] whitespace-pre-wrap leading-relaxed"
                        style={{ color: 'var(--fg-1)', fontFamily: 'var(--font-sans)' }}
                    >
                        {s.body || (
                            <span className="italic" style={{ color: 'var(--fg-3)' }}>
                                (empty body)
                            </span>
                        )}
                    </div>
                </Card>
            ))}
        </>
    );
}

function PassagesTab({ passages }: { passages: Passage[] }) {
    if (passages.length === 0) {
        return (
            <EmptyState
                title="No indexed passages for this report."
                detail="silver.document_passages is empty for the document chain backing this report. The §04p PDF stack chunks PDFs into passages — re-run ingest or check IngestQuality."
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
                            <span className="ml-auto" style={{ color: 'var(--fg-4)' }}>
                                {p.id.slice(0, 8)}
                            </span>
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

function MetadataTab({ report }: { report: ReportRow }) {
    const rows: Array<[string, string]> = [
        ['Report ID', report.report_id],
        ['Title', report.title],
        ['Company', report.company || '—'],
        ['Filing date', report.filing_date ? report.filing_date.slice(0, 10) : '—'],
        ['Commodity', report.commodity || '—'],
        ['Region', report.region || '—'],
        ['Source project', report.project_name || '—'],
        ['Version', String(report.version)],
        ['Ingested', shortDate(report.created_at)],
        ['Last updated', shortDate(report.updated_at)],
    ];
    return (
        <Card eyebrow="SILVER · REPORTS" title="Metadata">
            <div className="grid grid-cols-[180px_1fr] gap-y-2 text-[12px]">
                {rows.map(([k, v]) => (
                    <div key={k} className="contents">
                        <div
                            className="font-mono text-[10px] uppercase tracking-wider pt-0.5"
                            style={{ color: 'var(--fg-3)' }}
                        >
                            {k}
                        </div>
                        <div className="font-mono" style={{ color: 'var(--fg-0)' }}>
                            {v}
                        </div>
                    </div>
                ))}
            </div>
        </Card>
    );
}
