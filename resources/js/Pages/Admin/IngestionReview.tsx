import type { JSX } from 'react';
import { useEffect, useState } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

/**
 * /admin/ingestion-review — master-plan §3 Step 8 (doc-phase 58 scaffold).
 *
 * Read-only queue of silver.low_confidence_page_reviews rows. Each
 * page that the §04p quality graph routed to Silver Review appears
 * here with its reason code, status, and per-page confidence scores.
 *
 * Doc-phase 59+ will add the detail panel (rendered page image,
 * parser-used breakdown, accept/re-OCR/reject/annotate disposition
 * controls).
 */

interface QueueRow {
    review_item_id: string;
    report_id: string;
    page: number;
    workspace_id: string;
    reason: string;
    status: string;
    assigned_to: number | null;
    created_at: string;
    resolved_at: string | null;
    report_title: string | null;
    ocr_confidence: number | null;
    layout_confidence: number | null;
    table_confidence: number | null;
    parser_used: string | null;
    retry_count: number;
}

interface Summary {
    by_status: Record<string, number>;
    by_reason: Record<string, number>;
    total_pending: number;
    last_24h_new: number;
}

interface Filters {
    workspace_id: string | null;
    status: string | null;
    reason: string | null;
}

interface PageProps {
    queue: QueueRow[];
    filters: Filters;
    summary: Summary;
    available_reasons: string[];
    available_statuses: string[];
}

function StatusPill({ status }: { status: string }): JSX.Element {
    const styles: Record<string, string> = {
        pending: 'bg-amber-100 text-amber-800',
        assigned: 'bg-blue-100 text-blue-800',
        in_review: 'bg-indigo-100 text-indigo-800',
        resolved_accept: 'bg-green-100 text-green-800',
        resolved_reject: 'bg-red-100 text-red-800',
        resolved_reocr_requested: 'bg-purple-100 text-purple-800',
    };
    const cls = styles[status] ?? 'bg-gray-100 text-gray-700';
    return (
        <span className={`inline-block px-2 py-0.5 rounded text-xs font-mono ${cls}`}>
            {status}
        </span>
    );
}

function ConfidenceCell({ value }: { value: number | null }): JSX.Element {
    if (value === null) return <span className="text-gray-400 text-xs">—</span>;
    const pct = (value * 100).toFixed(1);
    let cls = 'text-red-700';
    if (value >= 0.85) cls = 'text-green-700';
    else if (value >= 0.5) cls = 'text-amber-700';
    return <span className={`font-mono text-xs ${cls}`}>{pct}%</span>;
}

function SummaryPanel({ summary }: { summary: Summary }): JSX.Element {
    return (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mb-4">
            <div className="border rounded p-3 bg-white shadow-sm">
                <div className="text-xs text-gray-500 uppercase tracking-wide">Pending review</div>
                <div className="text-2xl font-mono font-semibold mt-1">
                    {summary.total_pending.toLocaleString()}
                </div>
            </div>
            <div className="border rounded p-3 bg-white shadow-sm">
                <div className="text-xs text-gray-500 uppercase tracking-wide">New in last 24h</div>
                <div className="text-2xl font-mono font-semibold mt-1">
                    {summary.last_24h_new.toLocaleString()}
                </div>
            </div>
            <div className="border rounded p-3 bg-white shadow-sm">
                <div className="text-xs text-gray-500 uppercase tracking-wide">Top reason (pending)</div>
                <div className="text-sm font-mono mt-1">
                    {Object.entries(summary.by_reason).length === 0
                        ? <span className="text-gray-400">queue empty</span>
                        : Object.entries(summary.by_reason)
                            .sort(([, a], [, b]) => b - a)
                            .slice(0, 1)
                            .map(([reason, n]) => `${reason} (${n})`)
                            .join('')}
                </div>
            </div>
        </div>
    );
}

function FilterBar({
    filters,
    statuses,
    reasons,
}: {
    filters: Filters;
    statuses: string[];
    reasons: string[];
}): JSX.Element {
    function apply(next: Partial<Filters>): void {
        const merged = { ...filters, ...next };
        const params = Object.fromEntries(
            Object.entries(merged).filter(([, v]) => v !== null && v !== ''),
        );
        router.get('/admin/ingestion-review', params, {
            preserveState: false,
            preserveScroll: false,
        });
    }

    return (
        <div className="flex flex-wrap gap-3 items-end border rounded p-3 bg-gray-50 mb-4">
            <label className="flex flex-col text-xs">
                <span className="text-gray-600 mb-1">Status</span>
                <select
                    className="border rounded px-2 py-1 text-sm"
                    value={filters.status ?? ''}
                    onChange={(e) => apply({ status: e.target.value || null })}
                >
                    <option value="">all</option>
                    {statuses.map((s) => (
                        <option key={s} value={s}>{s}</option>
                    ))}
                </select>
            </label>
            <label className="flex flex-col text-xs">
                <span className="text-gray-600 mb-1">Reason</span>
                <select
                    className="border rounded px-2 py-1 text-sm"
                    value={filters.reason ?? ''}
                    onChange={(e) => apply({ reason: e.target.value || null })}
                >
                    <option value="">all</option>
                    {reasons.map((r) => (
                        <option key={r} value={r}>{r}</option>
                    ))}
                </select>
            </label>
            <label className="flex flex-col text-xs">
                <span className="text-gray-600 mb-1">Workspace UUID</span>
                <input
                    type="text"
                    className="border rounded px-2 py-1 text-sm font-mono w-72"
                    placeholder="optional — filter to one workspace"
                    value={filters.workspace_id ?? ''}
                    onChange={(e) => apply({ workspace_id: e.target.value || null })}
                />
            </label>
            {(filters.status || filters.reason || filters.workspace_id) && (
                <button
                    type="button"
                    className="text-xs text-blue-600 hover:underline self-end pb-1"
                    onClick={() => apply({ status: null, reason: null, workspace_id: null })}
                >
                    clear filters
                </button>
            )}
        </div>
    );
}

interface DetailPayload {
    review: {
        review_item_id: string;
        report_id: string;
        page: number;
        workspace_id: string;
        reason: string;
        status: string;
        assigned_to: number | null;
        created_at: string;
        resolved_at: string | null;
        resolution_notes: string | null;
    };
    report: {
        report_id: string;
        title: string | null;
        company: string | null;
        filing_date: string | null;
    };
    page_quality: {
        ocr_confidence: number | null;
        layout_confidence: number | null;
        table_confidence: number | null;
        parser_used: string | null;
        retry_count: number;
        deskew_applied: boolean;
        rotation_applied: number | null;
    };
    document_quality: {
        total_pages: number;
        low_confidence_pages: number;
        overall_quality_score: number | null;
        recommended_action: string;
    } | null;
    extractions: Array<{
        region: number;
        bbox: unknown;
        source_method: string;
        extraction_confidence: number | null;
        text_content: string | null;
        payload: Record<string, unknown> | null;
    }>;
    ocr_results: Array<{
        region: number;
        bbox: unknown;
        source_method: string;
        extraction_confidence: number | null;
        ocr_text: string;
        language_hint: string | null;
        payload: Record<string, unknown> | null;
    }>;
    layouts: Array<{
        region: number;
        bbox: unknown;
        source_method: string;
        extraction_confidence: number | null;
        layout_label: string;
        payload: Record<string, unknown> | null;
    }>;
    parser_runs: Array<{
        run_id: string;
        parser_used: string;
        parser_version: string;
        raw_output_uri: string | null;
        errors: unknown;
        warnings: unknown;
        started_at: string;
        finished_at: string | null;
    }>;
    page_render_url: string;
}

function DetailPanel({
    reviewItemId,
    onClose,
    onDispositionApplied,
}: {
    reviewItemId: string | null;
    onClose: () => void;
    onDispositionApplied: () => void;
}): JSX.Element | null {
    const [data, setData] = useState<DetailPayload | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [renderFailed, setRenderFailed] = useState(false);

    useEffect(() => {
        if (!reviewItemId) {
            setData(null);
            setError(null);
            setRenderFailed(false);
            return;
        }
        setData(null);
        setError(null);
        setRenderFailed(false);
        fetch(`/admin/ingestion-review/${reviewItemId}.json`, {
            credentials: 'same-origin',
            headers: { Accept: 'application/json' },
        })
            .then(async (resp) => {
                if (!resp.ok) {
                    throw new Error(`HTTP ${resp.status}`);
                }
                return resp.json() as Promise<DetailPayload>;
            })
            .then(setData)
            .catch((err) => setError(String(err)));
    }, [reviewItemId]);

    if (!reviewItemId) return null;

    return (
        <div className="fixed inset-0 z-40 flex" role="dialog" aria-modal="true">
            <button
                type="button"
                className="absolute inset-0 bg-black/40"
                aria-label="Close detail panel"
                onClick={onClose}
            />
            <div className="ml-auto w-full max-w-4xl bg-white shadow-xl overflow-y-auto relative z-10">
                <div className="sticky top-0 bg-white border-b px-6 py-3 flex items-center justify-between z-10">
                    <div>
                        <h2 className="text-lg font-semibold">Review item detail</h2>
                        <p className="text-xs text-gray-500 font-mono">{reviewItemId}</p>
                    </div>
                    <button
                        type="button"
                        onClick={onClose}
                        className="text-sm text-gray-600 hover:text-gray-900"
                    >
                        Close
                    </button>
                </div>

                {error && (
                    <div className="p-6 text-sm text-red-700 bg-red-50 border-b">
                        Failed to load: {error}
                    </div>
                )}

                {!data && !error && (
                    <div className="p-6 text-sm text-gray-500">Loading…</div>
                )}

                {data && (
                    <div className="p-6 space-y-6">
                        {/* Header strip */}
                        <div className="grid grid-cols-2 gap-4 text-sm">
                            <div>
                                <div className="text-xs text-gray-500 uppercase">Report</div>
                                <div className="font-medium">{data.report.title ?? '(untitled)'}</div>
                                {data.report.company && (
                                    <div className="text-xs text-gray-500 mt-0.5">{data.report.company}</div>
                                )}
                            </div>
                            <div>
                                <div className="text-xs text-gray-500 uppercase">Page</div>
                                <div className="font-mono">{data.review.page}</div>
                                <div className="text-xs text-gray-500 mt-0.5">
                                    of {data.document_quality?.total_pages ?? '?'} total
                                </div>
                            </div>
                        </div>

                        {/* Two-column body: image + panels */}
                        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                            <div>
                                <div className="text-xs text-gray-500 uppercase mb-2">Rendered page</div>
                                {renderFailed ? (
                                    <div className="border rounded p-6 bg-gray-50 text-xs text-gray-500 text-center">
                                        Page render unavailable.
                                        <br />
                                        Likely cause: report was ingested before
                                        doc-phase 59 bronze-key tracking landed.
                                        Re-upload the PDF to enable rendering.
                                    </div>
                                ) : (
                                    <img
                                        src={data.page_render_url}
                                        alt={`Page ${data.review.page} of ${data.report.title ?? data.review.report_id}`}
                                        className="border rounded w-full bg-gray-50"
                                        onError={() => setRenderFailed(true)}
                                    />
                                )}
                            </div>

                            <div className="space-y-4">
                                <DispositionControls
                                    reviewItemId={data.review.review_item_id}
                                    currentStatus={data.review.status}
                                    onApplied={(newStatus) => {
                                        setData({
                                            ...data,
                                            review: { ...data.review, status: newStatus },
                                        });
                                        onDispositionApplied();
                                    }}
                                />
                                <ParserBreakdown data={data} />
                                <RetryLog runs={data.parser_runs} />
                                <ExtractedTextSection data={data} />
                            </div>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}

function DispositionControls({
    reviewItemId,
    currentStatus,
    onApplied,
}: {
    reviewItemId: string;
    currentStatus: string;
    onApplied: (newStatus: string) => void;
}): JSX.Element {
    const [pendingAction, setPendingAction] = useState<string | null>(null);
    const [notes, setNotes] = useState('');
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const isResolved = currentStatus.startsWith('resolved_');

    async function applyDisposition(targetStatus: string): Promise<void> {
        setSubmitting(true);
        setError(null);
        try {
            const csrfMeta = document.querySelector('meta[name="csrf-token"]');
            const csrf = csrfMeta?.getAttribute('content') ?? '';
            const resp = await fetch(`/admin/ingestion-review/${reviewItemId}`, {
                method: 'PATCH',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    Accept: 'application/json',
                    'X-CSRF-TOKEN': csrf,
                    'X-Requested-With': 'XMLHttpRequest',
                },
                body: JSON.stringify({
                    status: targetStatus,
                    resolution_notes: notes.trim() || null,
                }),
            });
            if (!resp.ok) {
                const body = await resp.json().catch(() => ({}));
                throw new Error(body.error ?? `HTTP ${resp.status}`);
            }
            onApplied(targetStatus);
            setPendingAction(null);
            setNotes('');
        } catch (err) {
            setError(String(err));
        } finally {
            setSubmitting(false);
        }
    }

    if (isResolved) {
        return (
            <div className="border rounded p-3 bg-gray-50">
                <h3 className="text-sm font-semibold text-gray-700 mb-1">Disposition</h3>
                <p className="text-xs text-gray-600">
                    This item is resolved (<span className="font-mono">{currentStatus}</span>).
                    Resolved items are terminal — no further transitions allowed.
                </p>
            </div>
        );
    }

    const actions: Array<{ key: string; label: string; status: string; tone: string }> = [
        { key: 'accept', label: 'Accept', status: 'resolved_accept', tone: 'bg-green-600 hover:bg-green-700' },
        { key: 'reocr', label: 'Re-OCR requested', status: 'resolved_reocr_requested', tone: 'bg-blue-600 hover:bg-blue-700' },
        { key: 'reject', label: 'Reject', status: 'resolved_reject', tone: 'bg-red-600 hover:bg-red-700' },
    ];

    return (
        <div className="border rounded p-3 bg-white shadow-sm">
            <h3 className="text-sm font-semibold text-gray-700 mb-2">Disposition</h3>

            {error && (
                <div className="text-xs text-red-700 bg-red-50 border border-red-200 rounded p-2 mb-2">
                    {error}
                </div>
            )}

            {pendingAction === null ? (
                <div className="flex flex-wrap gap-2">
                    {actions.map((a) => (
                        <button
                            key={a.key}
                            type="button"
                            onClick={() => setPendingAction(a.key)}
                            className={`text-white text-xs px-3 py-1.5 rounded ${a.tone}`}
                            disabled={submitting}
                        >
                            {a.label}
                        </button>
                    ))}
                </div>
            ) : (
                <div className="space-y-2">
                    {(() => {
                        const action = actions.find((a) => a.key === pendingAction)!;
                        return (
                            <>
                                <p className="text-xs text-gray-700">
                                    Confirm <span className="font-semibold">{action.label}</span> for this page?
                                </p>
                                <textarea
                                    value={notes}
                                    onChange={(e) => setNotes(e.target.value)}
                                    placeholder="Optional resolution notes (e.g. 'page was a blank cover sheet')"
                                    className="w-full border rounded px-2 py-1 text-xs"
                                    rows={3}
                                    maxLength={4000}
                                    disabled={submitting}
                                />
                                <div className="flex gap-2">
                                    <button
                                        type="button"
                                        onClick={() => applyDisposition(action.status)}
                                        className={`text-white text-xs px-3 py-1.5 rounded ${action.tone}`}
                                        disabled={submitting}
                                    >
                                        {submitting ? 'Applying…' : `Confirm ${action.label}`}
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => {
                                            setPendingAction(null);
                                            setNotes('');
                                            setError(null);
                                        }}
                                        className="border text-xs px-3 py-1.5 rounded hover:bg-gray-50"
                                        disabled={submitting}
                                    >
                                        Cancel
                                    </button>
                                </div>
                            </>
                        );
                    })()}
                </div>
            )}

            <p className="mt-2 text-xs text-gray-400">
                Re-OCR currently flags the page as needing re-processing.
                Auto-triggering a re-OCR workflow lands in doc-phase 62.
            </p>
        </div>
    );
}

function ParserBreakdown({ data }: { data: DetailPayload }): JSX.Element {
    const pq = data.page_quality;
    return (
        <div className="border rounded p-3 bg-white shadow-sm">
            <h3 className="text-sm font-semibold text-gray-700 mb-2">Parser + confidence</h3>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
                <dt className="text-gray-500">Parser</dt>
                <dd className="font-mono text-xs">{pq.parser_used ?? '—'}</dd>
                <dt className="text-gray-500">OCR confidence</dt>
                <dd><ConfidenceCell value={pq.ocr_confidence} /></dd>
                <dt className="text-gray-500">Layout confidence</dt>
                <dd><ConfidenceCell value={pq.layout_confidence} /></dd>
                <dt className="text-gray-500">Table confidence</dt>
                <dd><ConfidenceCell value={pq.table_confidence} /></dd>
                <dt className="text-gray-500">Retries</dt>
                <dd className="font-mono text-xs">{pq.retry_count}</dd>
                <dt className="text-gray-500">Deskew applied</dt>
                <dd className="font-mono text-xs">{pq.deskew_applied ? 'yes' : 'no'}</dd>
                <dt className="text-gray-500">Rotation</dt>
                <dd className="font-mono text-xs">{pq.rotation_applied ?? '—'}°</dd>
                <dt className="text-gray-500">Reason</dt>
                <dd className="font-mono text-xs">{data.review.reason}</dd>
                <dt className="text-gray-500">Status</dt>
                <dd><StatusPill status={data.review.status} /></dd>
            </dl>
        </div>
    );
}

function RetryLog({ runs }: { runs: DetailPayload['parser_runs'] }): JSX.Element {
    return (
        <div className="border rounded p-3 bg-white shadow-sm">
            <h3 className="text-sm font-semibold text-gray-700 mb-2">Parser run history</h3>
            {runs.length === 0 ? (
                <p className="text-xs text-gray-500">No parser runs recorded.</p>
            ) : (
                <ol className="space-y-2 text-xs">
                    {runs.map((r) => {
                        const errors = Array.isArray(r.errors) ? r.errors : [];
                        const warnings = Array.isArray(r.warnings) ? r.warnings : [];
                        return (
                            <li key={r.run_id} className="border-b last:border-0 pb-1">
                                <div className="font-mono">
                                    {r.parser_used} · {r.parser_version}
                                </div>
                                <div className="text-gray-500">
                                    {new Date(r.started_at).toLocaleString()}
                                    {r.finished_at ? ` → ${new Date(r.finished_at).toLocaleString()}` : ''}
                                </div>
                                {errors.length > 0 && (
                                    <div className="text-red-700 mt-0.5">errors: {errors.length}</div>
                                )}
                                {warnings.length > 0 && (
                                    <div className="text-amber-700 mt-0.5">warnings: {warnings.length}</div>
                                )}
                            </li>
                        );
                    })}
                </ol>
            )}
        </div>
    );
}

function ExtractedTextSection({ data }: { data: DetailPayload }): JSX.Element {
    const items = data.ocr_results.length > 0
        ? data.ocr_results.map((r) => ({ region: r.region, text: r.ocr_text, conf: r.extraction_confidence }))
        : data.extractions.map((r) => ({ region: r.region, text: r.text_content ?? '', conf: r.extraction_confidence }));

    return (
        <div className="border rounded p-3 bg-white shadow-sm">
            <h3 className="text-sm font-semibold text-gray-700 mb-2">
                Extracted text · {items.length} region{items.length === 1 ? '' : 's'}
            </h3>
            {items.length === 0 ? (
                <p className="text-xs text-gray-500">No text extracted for this page.</p>
            ) : (
                <div className="space-y-2 max-h-96 overflow-y-auto text-xs">
                    {items.map((item) => (
                        <div key={item.region} className="border-l-2 border-gray-200 pl-2">
                            <div className="text-gray-500 font-mono">
                                region {item.region} · conf <ConfidenceCell value={item.conf} />
                            </div>
                            <div className="whitespace-pre-wrap text-gray-800 mt-0.5">
                                {item.text}
                            </div>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}

function QueueTable({
    queue,
    onRowClick,
}: {
    queue: QueueRow[];
    onRowClick: (reviewItemId: string) => void;
}): JSX.Element {
    if (queue.length === 0) {
        return (
            <div className="border rounded p-8 bg-white text-center text-gray-500">
                No review items match the current filters.
            </div>
        );
    }

    return (
        <div className="border rounded overflow-x-auto bg-white shadow-sm">
            <table className="w-full text-sm">
                <thead>
                    <tr className="text-left text-gray-500 border-b bg-gray-50">
                        <th className="p-2">Report</th>
                        <th className="p-2 text-right">Page</th>
                        <th className="p-2">Reason</th>
                        <th className="p-2">Status</th>
                        <th className="p-2">Parser</th>
                        <th className="p-2 text-right">OCR conf</th>
                        <th className="p-2 text-right">Layout conf</th>
                        <th className="p-2 text-right">Retries</th>
                        <th className="p-2">Created</th>
                    </tr>
                </thead>
                <tbody>
                    {queue.map((row) => (
                        <tr
                            key={row.review_item_id}
                            className="border-b last:border-0 hover:bg-blue-50 cursor-pointer"
                            onClick={() => onRowClick(row.review_item_id)}
                        >
                            <td className="p-2">
                                <div className="font-medium text-gray-900 truncate max-w-xs" title={row.report_title ?? row.report_id}>
                                    {row.report_title ?? '(untitled)'}
                                </div>
                                <div className="font-mono text-xs text-gray-400 truncate max-w-xs" title={row.report_id}>
                                    {row.report_id.slice(0, 8)}…
                                </div>
                            </td>
                            <td className="p-2 text-right font-mono">{row.page}</td>
                            <td className="p-2 font-mono text-xs">{row.reason}</td>
                            <td className="p-2"><StatusPill status={row.status} /></td>
                            <td className="p-2 font-mono text-xs text-gray-700">{row.parser_used ?? '—'}</td>
                            <td className="p-2 text-right"><ConfidenceCell value={row.ocr_confidence} /></td>
                            <td className="p-2 text-right"><ConfidenceCell value={row.layout_confidence} /></td>
                            <td className="p-2 text-right font-mono text-xs">{row.retry_count}</td>
                            <td className="p-2 text-xs text-gray-500 whitespace-nowrap">
                                {new Date(row.created_at).toLocaleString()}
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}

export default function IngestionReview({
    queue,
    filters,
    summary,
    available_reasons,
    available_statuses,
}: PageProps): JSX.Element {
    const [selectedItem, setSelectedItem] = useState<string | null>(null);

    // Phase 2 real-time push — multi-operator queue sync. The existing
    // IngestionReviewDispositionChanged event still fires for the in-place
    // status patching the doc-phase 64 spec calls out; the new
    // AdminSurfaceUpdated event (dispatched alongside it from
    // IngestionReviewController::update) drives the queue/summary reload
    // so other admins viewing the page see new items and resolved counts
    // appear without manual refresh.
    useAdminSurfaceUpdated('ingestion-review', null, () => {
        router.reload({ only: ['queue', 'summary'] });
    });

    return (
        <AppLayout>
            <Head title="Silver Review Queue" />
            <div className="max-w-7xl mx-auto p-6">
                <div className="flex items-baseline justify-between mb-4">
                    <h1 className="text-2xl font-semibold">Silver Review Queue</h1>
                    <div className="text-xs text-gray-500">
                        master-plan §3 Step 8 · doc-phase 60 detail panel
                    </div>
                </div>

                <SummaryPanel summary={summary} />
                <FilterBar
                    filters={filters}
                    statuses={available_statuses}
                    reasons={available_reasons}
                />
                <QueueTable queue={queue} onRowClick={setSelectedItem} />

                <p className="mt-4 text-xs text-gray-500">
                    Showing up to 200 most recent items, pending sorted first.
                    Click a row to view detail. Disposition controls
                    (accept / re-OCR / reject) land in doc-phase 61.
                </p>
            </div>

            <DetailPanel
                reviewItemId={selectedItem}
                onClose={() => setSelectedItem(null)}
                onDispositionApplied={() => {
                    // Refresh the queue list so the row's new status
                    // reflects without requiring a full page reload.
                    router.reload({ only: ['queue', 'summary'] });
                }}
            />
        </AppLayout>
    );
}
