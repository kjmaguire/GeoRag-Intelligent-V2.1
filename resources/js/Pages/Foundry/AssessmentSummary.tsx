import { useMemo, useState } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, EmptyState } from '@/Components/Foundry/primitives';

// ---------------------------------------------------------------------------
// CC-01 Item 5 — Assessment report structured summary page
// ---------------------------------------------------------------------------

type SectionId =
    | 'property_project'
    | 'location'
    | 'commodities'
    | 'operator'
    | 'year'
    | 'work_performed'
    | 'qa_qc'
    | 'recommendations'
    | 'other';

interface Claim {
    claim_text: string;
    page: number;
    bbox: [number, number, number, number];
    confidence: number;
}

interface SummarySection {
    section_id: SectionId;
    title: string;
    summary_text: string;
    claims: Claim[];
    page_range: [number, number] | null;
}

interface CompletenessItem {
    section_id: SectionId;
    expected: boolean;
    found: boolean;
    notes: string | null;
}

interface CompletenessChecklist {
    expected_sections: SectionId[];
    found_sections: SectionId[];
    missing_sections: SectionId[];
    items: CompletenessItem[];
}

interface Summary {
    summary_id: string;
    sections: SummarySection[];
    completeness_checklist: CompletenessChecklist;
    mean_claim_confidence: number | null;
    model_id: string;
    model_backend: string;
    generated_at: string;
}

type FindingKind =
    | 'work_types_undocumented'
    | 'coords_unmappable'
    | 'qaqc_described_incomplete'
    | 'prior_recommendations_orphaned'
    | 'attachments_referenced_missing';

type Severity = 'error' | 'warn' | 'info';

interface CompletenessFinding {
    finding_kind: FindingKind | string;
    severity: Severity | string;
    description: string;
    source_page: number | null;
    evidence: Record<string, unknown>;
}

interface CompletenessAudit {
    finding_run_id: string;
    created_at: string;
    counts: { error: number; warn: number; info: number };
    findings: CompletenessFinding[];
}

interface Props {
    project: { project_id: string; project_name: string; slug: string };
    report: {
        report_id: string;
        title: string;
        company: string;
        filing_date: string;
        commodity: string;
        pdf_id: string | null;
    };
    summary: Summary | null;
    can_regenerate: boolean;
    completeness_audit: CompletenessAudit | null;
}

const FINDING_KIND_LABELS: Record<string, string> = {
    work_types_undocumented: 'Work types undocumented',
    coords_unmappable: 'Coordinates unmappable',
    qaqc_described_incomplete: 'QA/QC described but incomplete',
    prior_recommendations_orphaned: 'Prior recommendations orphaned',
    attachments_referenced_missing: 'Attachments referenced missing',
};

function humaniseFindingKind(kind: string): string {
    return FINDING_KIND_LABELS[kind] ?? kind.replace(/_/g, ' ');
}

function severityTone(sev: string): 'danger' | 'warn' | 'info' {
    if (sev === 'error') return 'danger';
    if (sev === 'warn') return 'warn';
    return 'info';
}

function severityColor(sev: string): string {
    if (sev === 'error') return 'var(--danger)';
    if (sev === 'warn') return 'var(--warn)';
    return 'var(--info)';
}

const SEVERITY_ORDER: Severity[] = ['error', 'warn', 'info'];

function confidenceColor(c: number): string {
    if (c >= 0.8) return 'var(--ok)';
    if (c >= 0.5) return 'var(--warn)';
    return 'var(--err)';
}

function formatPageRange(r: [number, number] | null): string {
    if (r === null) return 'not found';
    return r[0] === r[1] ? `p. ${r[0]}` : `pp. ${r[0]}–${r[1]}`;
}

export default function AssessmentSummary({
    project,
    report,
    summary,
    can_regenerate,
    completeness_audit,
}: Props) {
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [auditBusy, setAuditBusy] = useState(false);
    const [auditError, setAuditError] = useState<string | null>(null);

    const sectionsById = useMemo(() => {
        const m = new Map<SectionId, SummarySection>();
        summary?.sections.forEach((s) => m.set(s.section_id, s));
        return m;
    }, [summary]);

    const onRunAudit = () => {
        setAuditBusy(true);
        setAuditError(null);
        router.post(
            route('foundry.reports.completeness-audit.run', {
                slug: project.slug,
                report_id: report.report_id,
            }),
            {},
            {
                preserveScroll: true,
                onError: (errors) => {
                    setAuditError(
                        typeof errors === 'object' && errors !== null
                            ? Object.values(errors).join(' ')
                            : 'Audit run failed',
                    );
                },
                onFinish: () => {
                    setAuditBusy(false);
                    router.reload({ only: ['summary', 'completeness_audit'] });
                },
            },
        );
    };

    const onRegenerate = () => {
        setBusy(true);
        setError(null);
        router.post(
            route('foundry.reports.assessment-summary.regenerate', {
                slug: project.slug,
                report_id: report.report_id,
            }),
            {},
            {
                preserveScroll: true,
                onError: (errors) => {
                    setError(
                        typeof errors === 'object' && errors !== null
                            ? Object.values(errors).join(' ')
                            : 'Regeneration failed',
                    );
                },
                onFinish: () => {
                    setBusy(false);
                    router.reload({ only: ['summary'] });
                },
            },
        );
    };

    return (
        <AppLayout>
            <Head title={`Assessment summary · ${report.title}`} />

            <div
                className="flex-1 overflow-y-auto"
                style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}
            >
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · ASSESSMENT SUMMARY`}
                    title={report.title}
                    sub={[
                        report.company || '—',
                        report.filing_date || '—',
                        report.commodity || '—',
                    ]}
                    actions={
                        <button
                            type="button"
                            disabled={!can_regenerate || busy}
                            onClick={onRegenerate}
                            className="px-3 py-1.5 text-sm rounded border"
                            style={{
                                borderColor: 'var(--border-1)',
                                opacity: !can_regenerate || busy ? 0.5 : 1,
                            }}
                        >
                            {busy ? 'Regenerating…' : 'Regenerate summary'}
                        </button>
                    }
                />

                {error && (
                    <div
                        className="mx-6 my-3 p-3 text-sm rounded"
                        style={{ background: 'var(--err-bg)', color: 'var(--err)' }}
                    >
                        {error}
                    </div>
                )}

                {summary === null ? (
                    <div className="px-6 py-4">
                        <EmptyState
                            title="No summary generated yet"
                            detail={
                                can_regenerate
                                    ? 'Click "Regenerate summary" to extract the structured sections from this report.'
                                    : 'This report has no linked bronze PDF — assessment summary cannot be generated.'
                            }
                        />
                    </div>
                ) : (
                    <div className="px-6 pb-12 grid grid-cols-1 lg:grid-cols-[1fr_280px] gap-6">
                        {/* Section column */}
                        <div className="flex flex-col gap-3">
                            {(summary.completeness_checklist.expected_sections as SectionId[]).map(
                                (sid) => {
                                    const section = sectionsById.get(sid);
                                    return (
                                        <SectionCard
                                            key={sid}
                                            sectionId={sid}
                                            section={section ?? null}
                                            pdfId={report.pdf_id}
                                        />
                                    );
                                },
                            )}
                        </div>

                        {/* Right sidebar — completeness + metadata */}
                        <aside className="flex flex-col gap-3">
                            <Card title="Completeness checklist">
                                <ul className="text-sm space-y-1.5">
                                    {summary.completeness_checklist.items.map((it) => (
                                        <li
                                            key={it.section_id}
                                            className="flex items-start justify-between gap-2"
                                        >
                                            <span style={{ color: 'var(--fg-2)' }}>
                                                {it.section_id.replace(/_/g, ' ')}
                                            </span>
                                            <Pill
                                                tone={it.found ? 'info' : 'warn'}
                                            >
                                                {it.found ? 'found' : 'missing'}
                                            </Pill>
                                        </li>
                                    ))}
                                </ul>
                                {summary.completeness_checklist.missing_sections.length > 0 && (
                                    <p
                                        className="mt-3 text-xs"
                                        style={{ color: 'var(--fg-3)' }}
                                    >
                                        Missing sections were not found by the v1 heading
                                        scan — re-run after improving the document's OCR
                                        or run a manual review.
                                    </p>
                                )}
                            </Card>

                            <CompletenessAuditCard
                                audit={completeness_audit}
                                pdfId={report.pdf_id}
                                busy={auditBusy}
                                error={auditError}
                                onRun={onRunAudit}
                            />

                            <Card title="Run metadata">
                                <dl className="text-xs space-y-1.5">
                                    <Meta
                                        k="Mean confidence"
                                        v={
                                            summary.mean_claim_confidence === null
                                                ? '—'
                                                : `${(
                                                      summary.mean_claim_confidence * 100
                                                  ).toFixed(1)}%`
                                        }
                                    />
                                    <Meta k="Model" v={summary.model_id} />
                                    <Meta k="Backend" v={summary.model_backend} />
                                    <Meta
                                        k="Generated"
                                        v={summary.generated_at.slice(0, 16).replace('T', ' ')}
                                    />
                                </dl>
                            </Card>
                        </aside>
                    </div>
                )}
            </div>
        </AppLayout>
    );
}

// ---------------------------------------------------------------------------
// Section card — collapsible, lists claims with page+bbox links to PDF viewer
// ---------------------------------------------------------------------------

function SectionCard({
    sectionId,
    section,
    pdfId,
}: {
    sectionId: SectionId;
    section: SummarySection | null;
    pdfId: string | null;
}) {
    const [open, setOpen] = useState(false);

    const title =
        section?.title ?? sectionId.replace(/_/g, ' ');
    const found = section !== null && section.page_range !== null;

    return (
        <Card
            title={
                <div className="flex items-center justify-between w-full">
                    <span className="capitalize">{title}</span>
                    <span
                        className="text-xs ml-2"
                        style={{ color: 'var(--fg-3)' }}
                    >
                        {found ? formatPageRange(section!.page_range) : '—'}
                    </span>
                </div>
            }
        >
            {!found ? (
                <p className="text-sm" style={{ color: 'var(--fg-3)' }}>
                    No source pages found for this section.
                </p>
            ) : (
                <>
                    <p className="text-sm whitespace-pre-wrap" style={{ color: 'var(--fg-1)' }}>
                        {section!.summary_text || '(empty narrative — see claims below)'}
                    </p>

                    {section!.claims.length > 0 && (
                        <button
                            type="button"
                            onClick={() => setOpen((v) => !v)}
                            className="mt-3 text-xs underline"
                            style={{ color: 'var(--accent)' }}
                        >
                            {open
                                ? `Hide ${section!.claims.length} cited claims`
                                : `Show ${section!.claims.length} cited claims`}
                        </button>
                    )}

                    {open && (
                        <ul className="mt-3 space-y-2 text-sm">
                            {section!.claims.map((c, i) => (
                                <li
                                    key={i}
                                    className="border-l-2 pl-3"
                                    style={{ borderColor: confidenceColor(c.confidence) }}
                                >
                                    <div style={{ color: 'var(--fg-1)' }}>{c.claim_text}</div>
                                    <div
                                        className="text-xs mt-1 flex gap-3"
                                        style={{ color: 'var(--fg-3)' }}
                                    >
                                        <span>page {c.page}</span>
                                        <span>conf {(c.confidence * 100).toFixed(0)}%</span>
                                        {pdfId && (
                                            <a
                                                href={`/internal/pdf/render_page?pdf_id=${pdfId}&page=${c.page}`}
                                                target="_blank"
                                                rel="noreferrer"
                                                className="underline"
                                                style={{ color: 'var(--accent)' }}
                                            >
                                                view page
                                            </a>
                                        )}
                                    </div>
                                </li>
                            ))}
                        </ul>
                    )}
                </>
            )}
        </Card>
    );
}

function CompletenessAuditCard({
    audit,
    pdfId,
    busy,
    error,
    onRun,
}: {
    audit: CompletenessAudit | null;
    pdfId: string | null;
    busy: boolean;
    error: string | null;
    onRun: () => void;
}) {
    const grouped = useMemo(() => {
        const out: Record<Severity, CompletenessFinding[]> = {
            error: [],
            warn: [],
            info: [],
        };
        audit?.findings.forEach((f) => {
            const sev = (f.severity as Severity) in out
                ? (f.severity as Severity)
                : 'info';
            out[sev].push(f);
        });
        return out;
    }, [audit]);

    return (
        <Card
            title={
                <div className="flex items-center justify-between w-full">
                    <span>Completeness audit</span>
                    <button
                        type="button"
                        disabled={busy || pdfId === null}
                        onClick={onRun}
                        className="text-xs px-2 py-0.5 rounded border"
                        style={{
                            borderColor: 'var(--border-1)',
                            opacity: busy || pdfId === null ? 0.5 : 1,
                        }}
                    >
                        {busy ? 'Running…' : audit ? 'Re-run' : 'Run audit'}
                    </button>
                </div>
            }
        >
            {error && (
                <p className="mb-2 text-xs" style={{ color: 'var(--err)' }}>
                    {error}
                </p>
            )}

            {audit === null ? (
                <p className="text-xs" style={{ color: 'var(--fg-3)' }}>
                    No audit has been run for this report yet. The audit checks for
                    coordinates that can't be mapped, attachments referenced but not
                    uploaded, QA/QC described but incomplete, and prior recommendations
                    that were never followed up.
                </p>
            ) : audit.findings.length === 0 ? (
                <p className="text-xs" style={{ color: 'var(--fg-2)' }}>
                    No completeness issues detected in the latest run.
                </p>
            ) : (
                <>
                    <div className="flex gap-1.5 mb-3 text-[10px]">
                        {SEVERITY_ORDER.map((sev) => (
                            audit.counts[sev] > 0 && (
                                <Pill key={sev} tone={severityTone(sev)}>
                                    {audit.counts[sev]} {sev}
                                </Pill>
                            )
                        ))}
                    </div>
                    <ul className="space-y-2.5 text-sm">
                        {SEVERITY_ORDER.flatMap((sev) =>
                            grouped[sev].map((f, i) => (
                                <li
                                    key={`${sev}-${i}`}
                                    className="border-l-2 pl-2.5"
                                    style={{ borderColor: severityColor(f.severity) }}
                                >
                                    <div
                                        className="text-xs font-medium"
                                        style={{ color: 'var(--fg-1)' }}
                                    >
                                        {humaniseFindingKind(f.finding_kind)}
                                    </div>
                                    <div
                                        className="text-xs mt-0.5"
                                        style={{ color: 'var(--fg-2)' }}
                                    >
                                        {f.description}
                                    </div>
                                    <div
                                        className="text-[10px] mt-1 flex gap-2 items-center"
                                        style={{ color: 'var(--fg-3)' }}
                                    >
                                        {f.source_page !== null && (
                                            pdfId ? (
                                                <a
                                                    href={`/internal/pdf/render_page?pdf_id=${pdfId}&page=${f.source_page}`}
                                                    target="_blank"
                                                    rel="noreferrer"
                                                    className="underline"
                                                    style={{ color: 'var(--accent)' }}
                                                >
                                                    p. {f.source_page}
                                                </a>
                                            ) : (
                                                <span>p. {f.source_page}</span>
                                            )
                                        )}
                                        <EvidenceSummary evidence={f.evidence} />
                                    </div>
                                </li>
                            )),
                        )}
                    </ul>
                    <p
                        className="mt-3 text-[10px]"
                        style={{ color: 'var(--fg-3)' }}
                    >
                        Run {audit.finding_run_id.slice(0, 8)} ·{' '}
                        {audit.created_at.slice(0, 16).replace('T', ' ')}
                    </p>
                </>
            )}
        </Card>
    );
}

function EvidenceSummary({ evidence }: { evidence: Record<string, unknown> }) {
    const keys = Object.keys(evidence ?? {});
    if (keys.length === 0) return null;
    const parts = keys.slice(0, 3).map((k) => {
        const v = evidence[k];
        const display =
            typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean'
                ? String(v)
                : Array.isArray(v)
                  ? `[${v.length}]`
                  : '…';
        return `${k}: ${display}`;
    });
    return <span title={JSON.stringify(evidence)}>{parts.join(' · ')}</span>;
}

function Meta({ k, v }: { k: string; v: string }) {
    return (
        <div className="flex justify-between gap-2">
            <dt style={{ color: 'var(--fg-3)' }}>{k}</dt>
            <dd
                className="truncate max-w-[180px]"
                style={{ color: 'var(--fg-1)' }}
                title={v}
            >
                {v}
            </dd>
        </div>
    );
}
