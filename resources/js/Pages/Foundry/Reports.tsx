import { useEffect, useRef, useState } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import {
    PageHeader,
    Card,
    Pill,
    Stat,
    ProgressBar,
    EmptyState,
} from '@/Components/Foundry/primitives';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';
import DataQualityFlagsBadge, {
    type DataQualityFlagsBadgeData,
} from '@/Components/DataQualityFlagsBadge';
import { formatWhen } from '@/lib/time';

/**
 * Foundry Reports — the merged documents surface.
 *
 * Replaces four pages that were all views of the same two tables:
 * Foundry/Report (the list), Foundry/ReportView (one document's sections and
 * passages), Foundry/IngestQuality (the same list plus per-document passage
 * counts and a project-level review rollup) and Foundry/Corpus (the nav's
 * "Reader" — the same list again, plus a cross-document passage sample and an
 * entity-link rollup). A user asking "did my upload actually work" had to
 * visit all of them to find out.
 *
 * Master-detail: documents on the left carrying their own ingest status, the
 * reader on the right with Quality as one of its tabs, and the project-level
 * quality strip above both. With nothing selected the right pane shows the
 * corpus overview the /corpus page used to own. /reports/{id} deep-links
 * straight to a selection; /imports/quality and /corpus redirect here.
 */

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

/** One row of the master list — a document plus its own ingest status. */
export interface ReportListRow {
    report_id: string;
    title: string;
    company: string;
    filing_date: string;
    commodity: string;
    version: number;
    is_scanned: boolean;
    parse_quality_pct: number | null;
    sections_count: number;
    has_content: boolean;
    passages: number;
    embedded: number;
    status: 'ok' | 'warn' | 'error' | 'unassessed';
}

interface ReportDetail {
    report_id: string;
    title: string;
    company: string;
    filing_date: string;
    commodity: string;
    version: number;
    region: string;
    project_name: string;
    parse_quality_pct: number | null;
    is_scanned: boolean;
    page_count: number | null;
    parser_used: string;
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
    url: string;
    expires_at: string;
}

export interface QualityRollup {
    totals: {
        accepted: number;
        flagged: number;
        rejected: number;
        awaiting_ocr: number;
    };
    passages_total: number;
    embedded_total: number;
    documents: number;
    documents_not_retrievable: number;
    pass_gate: boolean;
}

export interface ProjectOverview {
    entity_links: number;
    entity_summary: Array<{ kind: string; count: number }>;
    recent_passages: Array<
        Passage & { report_id: string; report_title: string }
    >;
}

interface ReportsProps {
    project: { project_id: string; project_name: string; slug: string };
    reports: ReportListRow[];
    quality: QualityRollup;
    empty: boolean;

    selected_id: string | null;
    report: ReportDetail | null;
    sections: Section[];
    passages: Passage[];
    passages_total?: number;
    figures?: Figure[];
    data_quality_flags?: DataQualityFlagsBadgeData | null;
    /** Present only when no document is selected. */
    overview?: ProjectOverview | null;
}

type Tab = 'sections' | 'passages' | 'figures' | 'quality' | 'metadata';

const TABS: Array<{ id: Tab; label: string }> = [
    { id: 'sections', label: 'Sections' },
    { id: 'passages', label: 'Passages' },
    { id: 'figures', label: 'Figures' },
    { id: 'quality', label: 'Quality' },
    { id: 'metadata', label: 'Metadata' },
];

/** Props the detail pane owns — the only ones a selection change refetches. */
const DETAIL_PROPS = [
    'selected_id',
    'report',
    'sections',
    'passages',
    'passages_total',
    'figures',
    'data_quality_flags',
];

export default function FoundryReports({
    project,
    reports,
    quality,
    empty,
    selected_id,
    report,
    sections,
    passages,
    passages_total = 0,
    figures = [],
    data_quality_flags = null,
    overview = null,
}: ReportsProps) {
    // ingest_pdf re-runs, OCR re-runs and the embed sweep all change both
    // halves of this page: the list's per-document status and the open
    // document's sections/passages. Refresh both on 'reports'; 'quality'
    // only moves the rollup.
    useWorkspaceDataUpdated(project.project_id, (event) => {
        if (event.affected_types.includes('reports')) {
            router.reload({ only: ['reports', 'quality', 'empty', ...DETAIL_PROPS] });
        } else if (event.affected_types.includes('quality')) {
            router.reload({ only: ['quality'] });
        }
        if (event.affected_types.includes('data_quality_flags')) {
            router.reload({ only: ['data_quality_flags'] });
        }
    });

    // Chat's citation "Open in Reader →" link deep-links as `?section=<N>`,
    // where N is the raw section_number ReportResolver::resolve() returned off
    // the citation's source_chunk_id. sectionsFor() normalises sections_text
    // (keyed "1", "2", "preamble", ...) into `sections[].heading` holding that
    // same key, so a straight string match is exact. Read once on mount.
    const [highlightSection] = useState<string | null>(() => {
        if (typeof window === 'undefined') return null;
        return new URLSearchParams(window.location.search).get('section');
    });

    const selectedRow = reports.find((r) => r.report_id === selected_id) ?? null;

    return (
        <AppLayout>
            <Head
                title={
                    report
                        ? `${report.title} · ${project.project_name}`
                        : `Reports · ${project.project_name}`
                }
            />

            <div
                className="flex-1 flex flex-col overflow-hidden"
                style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}
            >
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · REPORTS`}
                    title="Documents & ingest quality"
                    sub={
                        empty
                            ? 'Nothing ingested yet'
                            : `${quality.documents} document${quality.documents === 1 ? '' : 's'} · ` +
                              `${quality.passages_total.toLocaleString()} passages · ` +
                              `${quality.embedded_total.toLocaleString()} embedded`
                    }
                    actions={<DataQualityFlagsBadge data={data_quality_flags} />}
                />

                {empty ? (
                    <div className="px-8 py-12">
                        <EmptyState
                            title="No documents linked to this project yet."
                            detail="Drop a PDF or XLSX filing into the Import Wizard — once ingested it lands in silver.reports, gets chunked into silver.document_passages, and shows up here with its ingest status."
                        />
                    </div>
                ) : (
                    <>
                        <QualityStrip quality={quality} />

                        <div className="flex-1 flex min-h-0">
                            <DocumentList
                                reports={reports}
                                slug={project.slug}
                                selectedId={selected_id}
                            />

                            <section className="flex-1 min-w-0 overflow-y-auto">
                                {report ? (
                                    <DetailPane
                                        report={report}
                                        row={selectedRow}
                                        sections={sections}
                                        passages={passages}
                                        passagesTotal={Math.max(passages_total, passages.length)}
                                        figures={figures}
                                        flags={data_quality_flags}
                                        highlightSection={highlightSection}
                                    />
                                ) : (
                                    <ProjectOverviewPane
                                        overview={overview}
                                        slug={project.slug}
                                    />
                                )}
                            </section>
                        </div>
                    </>
                )}
            </div>
        </AppLayout>
    );
}

/* ------------------------------------------------------------------ */
/* Right pane when nothing is selected (was the /corpus "Reader" page)  */
/* ------------------------------------------------------------------ */

function ProjectOverviewPane({
    overview,
    slug,
}: {
    overview: ProjectOverview | null;
    slug: string;
}) {
    if (!overview) {
        return (
            <div className="px-8 py-12">
                <EmptyState
                    title="Select a document."
                    detail="Pick a filing on the left to read its sections, inspect the indexed passages that chat retrieves from, and see its ingest quality."
                />
            </div>
        );
    }

    return (
        <div className="px-8 py-6 space-y-3">
            <Card
                eyebrow="CORPUS"
                title="What chat can see in this project"
            >
                <p className="text-[12px] mb-3" style={{ color: 'var(--fg-2)' }}>
                    Pick a document on the left to read it. Below is a sample of the
                    passages retrieval actually draws from, newest documents first.
                </p>
                {overview.entity_summary.length > 0 && (
                    <div className="flex items-center gap-1.5 flex-wrap">
                        <span
                            className="text-[10px] font-mono uppercase tracking-wider mr-1"
                            style={{ color: 'var(--fg-3)' }}
                        >
                            {overview.entity_links.toLocaleString()} entity links
                        </span>
                        {overview.entity_summary.map((e) => (
                            <Pill key={e.kind} tone="neutral">
                                {e.kind} {e.count.toLocaleString()}
                            </Pill>
                        ))}
                    </div>
                )}
            </Card>

            {overview.recent_passages.length === 0 ? (
                <EmptyState
                    title="No indexed passages in this project yet."
                    detail="Documents are listed on the left, but none of them has produced passages. Open one and check its Quality tab to see where ingest stopped."
                />
            ) : (
                <Card
                    eyebrow="SILVER · DOCUMENT_PASSAGES"
                    title={`${overview.recent_passages.length} recent passages across documents`}
                >
                    <div className="space-y-2">
                        {overview.recent_passages.map((p) => (
                            <Link
                                key={p.id}
                                href={`/projects/${slug}/reports/${p.report_id}`}
                                only={DETAIL_PROPS}
                                preserveState
                                preserveScroll
                                className="block p-3 rounded border"
                                style={{
                                    background: 'var(--bg-1)',
                                    borderColor: 'var(--line-1)',
                                }}
                            >
                                <div
                                    className="flex items-center gap-2 mb-2 text-[10px] font-mono uppercase tracking-wider"
                                    style={{ color: 'var(--fg-3)' }}
                                >
                                    <Pill tone="info">{p.report_title}</Pill>
                                    <Pill tone="neutral">ord {p.ordinal}</Pill>
                                    {p.page_first !== null && (
                                        <Pill tone="neutral">p.{p.page_first}</Pill>
                                    )}
                                </div>
                                <div
                                    className="text-[12px] whitespace-pre-wrap leading-relaxed line-clamp-3"
                                    style={{ color: 'var(--fg-1)' }}
                                >
                                    {p.text}
                                </div>
                            </Link>
                        ))}
                    </div>
                </Card>
            )}
        </div>
    );
}

/* ------------------------------------------------------------------ */
/* Project-level quality strip (was the top of Foundry/IngestQuality)   */
/* ------------------------------------------------------------------ */

function QualityStrip({ quality }: { quality: QualityRollup }) {
    const { totals } = quality;
    const total = totals.accepted + totals.flagged + totals.rejected;
    const acceptPct = total === 0 ? 0 : Math.round((totals.accepted / total) * 100);

    return (
        <>
            <section
                className="grid grid-cols-2 sm:grid-cols-4 gap-px px-8 py-5"
                style={{ background: 'var(--line-1)' }}
            >
                <Stat label="ACCEPTED" value={String(totals.accepted)} tone="accent" />
                <Stat
                    label="FLAGGED"
                    value={String(totals.flagged)}
                    sub={totals.flagged > 0 ? 'needs review' : 'clean'}
                />
                <Stat label="REJECTED" value={String(totals.rejected)} />
                <Stat
                    label="AWAITING OCR"
                    value={String(totals.awaiting_ocr)}
                    sub="Tier-2 pipeline"
                />
            </section>

            <section className="px-8 py-4 flex items-center gap-4">
                <div className="flex-1">
                    <div className="flex justify-between text-xs mb-1">
                        <span style={{ color: 'var(--fg-2)' }}>Acceptance rate</span>
                        <span className="font-mono" style={{ color: 'var(--fg-0)' }}>
                            {acceptPct}%
                        </span>
                    </div>
                    <ProgressBar
                        value={acceptPct}
                        tone={quality.pass_gate ? 'accent' : 'warn'}
                        height={8}
                    />
                </div>
                {quality.documents_not_retrievable > 0 && (
                    <Pill tone="danger" dot>
                        {quality.documents_not_retrievable} not retrievable
                    </Pill>
                )}
                {quality.pass_gate ? (
                    <Pill tone="accent" dot>
                        Bronze → Silver gate: PASS
                    </Pill>
                ) : (
                    <Pill tone="warn" dot>
                        Gate blocked
                    </Pill>
                )}
            </section>
        </>
    );
}

/* ------------------------------------------------------------------ */
/* Master list                                                          */
/* ------------------------------------------------------------------ */

function DocumentList({
    reports,
    slug,
    selectedId,
}: {
    reports: ReportListRow[];
    slug: string;
    selectedId: string | null;
}) {
    return (
        <nav
            className="w-[320px] shrink-0 overflow-y-auto border-r"
            style={{ borderColor: 'var(--line-1)' }}
            aria-label="Documents"
        >
            <div
                className="px-4 py-2 text-[10px] font-mono uppercase tracking-wider border-b sticky top-0"
                style={{
                    color: 'var(--fg-3)',
                    borderColor: 'var(--line-1)',
                    background: 'var(--bg-0)',
                }}
            >
                {reports.length} document{reports.length === 1 ? '' : 's'}
            </div>

            {reports.map((r) => {
                const active = r.report_id === selectedId;
                return (
                    <Link
                        key={r.report_id}
                        href={`/projects/${slug}/reports/${r.report_id}`}
                        // Only the detail pane changes, so ask for just those
                        // props and keep the list + rollup we already have.
                        // preserveScroll stops the long list jumping to top on
                        // every click.
                        only={DETAIL_PROPS}
                        preserveState
                        preserveScroll
                        aria-current={active ? 'true' : undefined}
                        className="block px-4 py-3 border-b"
                        style={{
                            borderColor: 'var(--line-1)',
                            background: active ? 'var(--accent-bg)' : 'transparent',
                            borderLeft: `2px solid ${active ? 'var(--accent)' : 'transparent'}`,
                        }}
                    >
                        <div
                            className="text-[12px] leading-snug mb-1.5"
                            style={{ color: active ? 'var(--accent)' : 'var(--fg-0)' }}
                        >
                            {r.title}
                        </div>
                        <div className="flex items-center gap-1.5 flex-wrap">
                            <Pill tone={statusToneFor(r.status)} dot>
                                {r.status === 'ok'
                                    ? 'indexed'
                                    : r.status === 'warn'
                                      ? 'partial'
                                      : r.status === 'error'
                                        ? 'not retrievable'
                                        : 'no passages'}
                            </Pill>
                            {r.passages > 0 && (
                                <span
                                    className="text-[10px] font-mono"
                                    style={{ color: 'var(--fg-3)' }}
                                >
                                    {r.embedded.toLocaleString()}/{r.passages.toLocaleString()}
                                </span>
                            )}
                            {r.is_scanned && <Pill tone="warn">scanned</Pill>}
                        </div>
                        {(r.company || r.filing_date) && (
                            <div
                                className="mt-1.5 text-[10px] font-mono truncate"
                                style={{ color: 'var(--fg-3)' }}
                            >
                                {[r.company, r.filing_date ? r.filing_date.slice(0, 10) : null]
                                    .filter(Boolean)
                                    .join(' · ')}
                            </div>
                        )}
                    </Link>
                );
            })}
        </nav>
    );
}

/* ------------------------------------------------------------------ */
/* Detail pane                                                          */
/* ------------------------------------------------------------------ */

function DetailPane({
    report,
    row,
    sections,
    passages,
    passagesTotal,
    figures,
    flags,
    highlightSection,
}: {
    report: ReportDetail;
    row: ReportListRow | null;
    sections: Section[];
    passages: Passage[];
    passagesTotal: number;
    figures: Figure[];
    flags: DataQualityFlagsBadgeData | null;
    highlightSection: string | null;
}) {
    // A `?section=` deep link only ever points at a real section, so Sections
    // already wins the priority order below — no extra branching needed.
    const initial: Tab =
        sections.length > 0
            ? 'sections'
            : passages.length > 0
              ? 'passages'
              : figures.length > 0
                ? 'figures'
                : 'quality';
    const [tab, setTab] = useState<Tab>(initial);

    // Selecting a different document swaps every prop below without
    // remounting this component, so the tab has to follow the new document
    // rather than stay on a tab that may now be empty.
    useEffect(() => {
        setTab(initial);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [report.report_id]);

    const metadataOnly = sections.length === 0 && passages.length === 0;

    return (
        <>
            <div className="px-8 pt-6 pb-3">
                <div className="text-[15px] mb-1" style={{ color: 'var(--fg-0)' }}>
                    {report.title}
                </div>
                <div className="text-[11px] font-mono" style={{ color: 'var(--fg-2)' }}>
                    {[
                        report.company,
                        report.filing_date ? report.filing_date.slice(0, 10) : null,
                        report.commodity,
                        `v${report.version}`,
                    ]
                        .filter(Boolean)
                        .join(' · ')}
                </div>
            </div>

            <div className="px-8">
                <div
                    className="flex items-center gap-1 border-b"
                    style={{ borderColor: 'var(--line-1)' }}
                >
                    {TABS.map((t) => {
                        const count =
                            t.id === 'sections'
                                ? sections.length
                                : t.id === 'passages'
                                  ? passagesTotal
                                  : t.id === 'figures'
                                    ? figures.length
                                    : t.id === 'quality'
                                      ? (flags?.open_total ?? 0)
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
                                        '2px solid ' +
                                        (tab === t.id ? 'var(--accent)' : 'transparent'),
                                }}
                            >
                                {t.label}
                                {count !== null && count > 0 && (
                                    <span style={{ color: 'var(--fg-3)', marginLeft: 6 }}>
                                        {count}
                                    </span>
                                )}
                            </button>
                        );
                    })}
                </div>
            </div>

            <div className="px-8 py-6 space-y-3">
                {tab === 'sections' && (
                    <SectionsTab
                        sections={sections}
                        metadataOnly={metadataOnly}
                        highlightHeading={highlightSection}
                    />
                )}
                {tab === 'passages' && <PassagesTab passages={passages} total={passagesTotal} />}
                {tab === 'figures' && <FiguresTab figures={figures} />}
                {tab === 'quality' && <QualityTab report={report} row={row} flags={flags} />}
                {tab === 'metadata' && <MetadataTab report={report} />}
            </div>
        </>
    );
}

/* ------------------------------------------------------------------ */
/* Tabs                                                                 */
/* ------------------------------------------------------------------ */

function QualityTab({
    report,
    row,
    flags,
}: {
    report: ReportDetail;
    row: ReportListRow | null;
    flags: DataQualityFlagsBadgeData | null;
}) {
    const passages = row?.passages ?? 0;
    const embedded = row?.embedded ?? 0;
    const status = row?.status ?? 'unassessed';

    return (
        <>
            <Card eyebrow="INGEST" title="Is this document retrievable?">
                <div className="flex items-center gap-2 mb-4">
                    <Pill tone={statusToneFor(status)} dot>
                        {status}
                    </Pill>
                    <span className="text-[12px]" style={{ color: 'var(--fg-2)' }}>
                        {status === 'ok'
                            ? 'Every passage is embedded — chat can retrieve from this document.'
                            : status === 'warn'
                              ? 'Some passages are not embedded yet. The embed sweep runs every 10 minutes; if this does not clear, check the ingest run.'
                              : status === 'error'
                                ? 'Passages exist but none are embedded, so chat cannot retrieve this document at all.'
                                : 'No passages were written for this document — the parse produced nothing to index.'}
                    </span>
                </div>

                <div className="grid grid-cols-[180px_1fr] gap-y-2 text-[12px]">
                    <MetaRow label="Passages" value={passages.toLocaleString()} />
                    <MetaRow
                        label="Embedded"
                        value={`${embedded.toLocaleString()}${
                            passages > 0
                                ? ` (${Math.round((embedded / passages) * 100)}%)`
                                : ''
                        }`}
                    />
                    <MetaRow label="Pages" value={report.page_count?.toLocaleString() ?? '—'} />
                    <MetaRow label="Scanned" value={report.is_scanned ? 'yes' : 'no'} />
                    {/*
                      * parse_quality_pct is a FRACTION of the 17-section NI 43-101
                      * baseline (pdf_report.py NI43_BASELINE_SECTIONS), not a
                      * percentage, and may exceed 1.0. It measures STRUCTURAL
                      * coverage, not extraction quality — a document that isn't
                      * shaped like an NI 43-101 scores low while having parsed
                      * perfectly — so it is reported here as a neutral fact and
                      * deliberately does not drive the status above.
                      */}
                    <MetaRow
                        label="NI 43-101 coverage"
                        value={
                            typeof report.parse_quality_pct === 'number'
                                ? `${Math.min(100, Math.round(report.parse_quality_pct * 100))}% of the 17-section baseline`
                                : '—'
                        }
                    />
                    {/*
                      * parser_used is the BASE parser label (PyMuPDF reads
                      * 'fitz' whenever a text layer was found); it is not the
                      * OCR engine. The real per-page engine lives in
                      * document_passages.ocr_method.
                      */}
                    <MetaRow label="Base parser" value={report.parser_used || '—'} />
                </div>
            </Card>

            {flags && flags.open_total > 0 && (
                <Card
                    eyebrow={`DATA QUALITY · ${flags.open_total} OPEN`}
                    title="Flags on this document"
                >
                    <div className="space-y-2">
                        {flags.flags.map((f) => (
                            <div
                                key={String(f.flag_id)}
                                className="flex items-start gap-2 text-[12px]"
                            >
                                <Pill
                                    tone={
                                        f.severity === 'ERROR'
                                            ? 'danger'
                                            : f.severity === 'WARNING'
                                              ? 'warn'
                                              : 'info'
                                    }
                                >
                                    {f.severity}
                                </Pill>
                                <div>
                                    <div
                                        className="font-mono text-[11px]"
                                        style={{ color: 'var(--fg-1)' }}
                                    >
                                        {f.flag_type}
                                    </div>
                                    <div style={{ color: 'var(--fg-2)' }}>{f.description}</div>
                                </div>
                            </div>
                        ))}
                    </div>
                </Card>
            )}
        </>
    );
}

function MetaRow({ label, value }: { label: string; value: string }) {
    return (
        <div className="contents">
            <div
                className="font-mono text-[10px] uppercase tracking-wider pt-0.5"
                style={{ color: 'var(--fg-3)' }}
            >
                {label}
            </div>
            <div className="font-mono" style={{ color: 'var(--fg-0)' }}>
                {value}
            </div>
        </div>
    );
}

function SectionsTab({
    sections,
    metadataOnly,
    highlightHeading,
}: {
    sections: Section[];
    metadataOnly: boolean;
    highlightHeading?: string | null;
}) {
    const highlightRef = useRef<HTMLDivElement | null>(null);
    useEffect(() => {
        if (highlightHeading && highlightRef.current) {
            highlightRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
        // Only run on mount for the deep-link landing.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    if (sections.length === 0) {
        return (
            <EmptyState
                title="No sections_text on this document yet."
                detail={
                    metadataOnly
                        ? "This silver.reports row exists as metadata only — the source filing was either not parsed yet, scanned-image (Tier 2 OCR pending), or the §04p PDF stack hasn't run on it. Once it runs, structured sections + chunked passages will populate here."
                        : 'Sections are empty but indexed passages exist — switch to the Passages tab.'
                }
            />
        );
    }
    return (
        <>
            {sections.map((s) => {
                const isHighlighted = Boolean(highlightHeading) && s.heading === highlightHeading;
                return (
                    <div
                        key={s.index}
                        ref={isHighlighted ? highlightRef : undefined}
                        style={
                            isHighlighted
                                ? { outline: '2px solid var(--accent)', borderRadius: 8 }
                                : undefined
                        }
                    >
                        <Card
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
                    </div>
                );
            })}
        </>
    );
}

function PassagesTab({ passages, total }: { passages: Passage[]; total: number }) {
    if (passages.length === 0) {
        return (
            <EmptyState
                title="No indexed passages for this document."
                detail="silver.document_passages holds no rows whose document_id is this report. The §04p PDF stack chunks PDFs into passages — re-run ingest or check the Quality tab."
            />
        );
    }
    const title =
        total > passages.length
            ? `${total} chunked passages (first ${passages.length})`
            : `${total} chunked passages`;
    return (
        <Card eyebrow="SILVER · DOCUMENT_PASSAGES" title={title}>
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
                                    {p.page_last && p.page_last !== p.page_first
                                        ? `-${p.page_last}`
                                        : ''}
                                </Pill>
                            )}
                            {p.chunk_kind && <Pill tone="neutral">{p.chunk_kind}</Pill>}
                            <span className="ml-auto" style={{ color: 'var(--fg-3)' }}>
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

function FiguresTab({ figures }: { figures: Figure[] }) {
    if (figures.length === 0) {
        return (
            <EmptyState
                title="No figures extracted from this document yet."
                detail={
                    'The current §04p ingest pipeline preserves text, tables, page ' +
                    'coordinates, and OCR provenance. Automated figure-region ' +
                    'extraction is not currently enabled.'
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

function MetadataTab({ report }: { report: ReportDetail }) {
    const rows: Array<[string, string]> = [
        ['Report ID', report.report_id],
        ['Title', report.title],
        ['Company', report.company || '—'],
        ['Filing date', report.filing_date ? report.filing_date.slice(0, 10) : '—'],
        ['Commodity', report.commodity || '—'],
        ['Region', report.region || '—'],
        ['Source project', report.project_name || '—'],
        ['Version', String(report.version)],
        ['Ingested', formatWhen(report.created_at)],
        ['Last updated', formatWhen(report.updated_at)],
    ];
    return (
        <Card eyebrow="SILVER · REPORTS" title="Metadata">
            <div className="grid grid-cols-[180px_1fr] gap-y-2 text-[12px]">
                {rows.map(([k, v]) => (
                    <MetaRow key={k} label={k} value={v} />
                ))}
            </div>
        </Card>
    );
}

function statusToneFor(s: string): 'accent' | 'warn' | 'danger' | 'info' | 'neutral' {
    if (s === 'ok') return 'accent';
    if (s === 'warn') return 'warn';
    if (s === 'error') return 'danger';
    return 'neutral';
}
