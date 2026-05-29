import type { JSX } from 'react';
import { useEffect, useState } from 'react';

/**
 * §19.2 Trust Inspector drawer.
 *
 * Aggregates everything the geologist needs to decide whether to
 * trust the current answer:
 *   1. Confidence verdict (high / medium / low)
 *   2. Sources (per-store + per-state citation counts)
 *   3. Evidence drill-in (per-citation list with confidence + state)
 *   4. Retrieval stages (what was fetched + ranking signals)
 *   5. Missing data (refusals, truncation, unresolved citations)
 *   6. Conflicts (link to per-run conflicts endpoint)
 *   7. Assumptions (LLM's inferred priors — surfaced from layer-2 output in v2)
 *
 * Backend: GET /api/v1/answer-runs/{id}/trust-summary (Laravel proxy).
 */

interface TrustSummary {
    answer_run_id: string;
    query_text: string;
    query_class: string;
    model_name: string | null;
    citation_lifecycle_state: string | null;
    citation_mode: string | null;
    partial_resolution_rate: number | null;
    rejection_reason: string | null;
    created_at: string | null;
    data_version: number;
    citations: {
        total: number;
        resolved: number;
        resolution_pct: number;
        per_kind_state: Array<{ source_store: string; state: string; n: number }>;
    };
    retrieval: {
        per_stage: Array<{
            stage: string;
            n: number;
            avg_retriever: number | null;
            avg_reranker: number | null;
            included: number;
            used_in_citation: number;
        }>;
    };
    sources: Array<{
        citation_id: string;
        source_store: string;
        marker_text: string;
        confidence: number;
        evidence_id: string;
        state: string;
        rejection_reason: string | null;
    }>;
    confidence_summary: {
        resolution_pct: number;
        lifecycle: string | null;
        verdict: 'high' | 'medium' | 'low';
    };
    missing_data: string[];
    conflicts: {
        count: number;
        see_endpoint: string;
    };
    assumptions: string[];
    feedback: Array<{
        polarity: string;
        category: string | null;
        note: string | null;
        created_at: string;
    }>;
    provenance: {
        trace_id: string | null;
        lookup_endpoint: string;
    };
}

interface Props {
    open: boolean;
    onClose: () => void;
    answerRunId: string | null;
    projectId?: string | null;
    onOpenEvidence?: (evidenceId: string) => void;
}

function verdictBadge(verdict: 'high' | 'medium' | 'low'): JSX.Element {
    const cfg = {
        high:   { bg: 'bg-emerald-100', fg: 'text-emerald-800', label: '✓ High confidence' },
        medium: { bg: 'bg-amber-100',   fg: 'text-amber-800',   label: '~ Medium confidence' },
        low:    { bg: 'bg-red-100',     fg: 'text-red-800',     label: '! Low confidence' },
    }[verdict];
    return (
        <span className={`inline-flex items-center rounded px-2.5 py-1 text-sm font-medium ${cfg.bg} ${cfg.fg}`}>
            {cfg.label}
        </span>
    );
}

function SectionHeading({ children }: { children: React.ReactNode }): JSX.Element {
    return (
        <h3 className="mt-4 text-xs font-semibold uppercase tracking-wide text-zinc-500">
            {children}
        </h3>
    );
}

export function TrustInspector({ open, onClose, answerRunId, projectId, onOpenEvidence }: Props): JSX.Element | null {
    const [data, setData] = useState<TrustSummary | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        if (!open || !answerRunId) return;
        setLoading(true);
        setError(null);
        setData(null);
        const url = `/api/v1/answer-runs/${answerRunId}/trust-summary${projectId ? `?project_id=${projectId}` : ''}`;
        fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
            .then(async (r) => {
                if (!r.ok) throw new Error(await r.text());
                return r.json();
            })
            .then((j: TrustSummary) => setData(j))
            .catch((e) => setError(e.message || String(e)))
            .finally(() => setLoading(false));
    }, [open, answerRunId, projectId]);

    if (!open) return null;

    return (
        <div
            className="fixed inset-0 z-50 flex"
            role="dialog"
            aria-modal="true"
            aria-labelledby="trust-inspector-title"
        >
            <div
                className="absolute inset-0 bg-zinc-900/40"
                onClick={onClose}
                aria-hidden="true"
            />
            <aside
                className="ml-auto h-full w-full max-w-xl translate-x-0 overflow-y-auto bg-white shadow-2xl"
            >
                <div className="sticky top-0 z-10 border-b border-zinc-200 bg-white px-5 py-3">
                    <div className="flex items-center justify-between">
                        <h2 id="trust-inspector-title" className="text-lg font-semibold text-zinc-900">
                            Trust Inspector
                        </h2>
                        <button
                            type="button"
                            onClick={onClose}
                            className="rounded p-1 text-zinc-500 hover:bg-zinc-100"
                            aria-label="Close"
                        >
                            ✕
                        </button>
                    </div>
                    {data && (
                        <div className="mt-2 flex items-center gap-3">
                            {verdictBadge(data.confidence_summary.verdict)}
                            <span className="text-xs text-zinc-500">
                                {data.citations.resolved}/{data.citations.total} citations resolved · {data.confidence_summary.resolution_pct}%
                            </span>
                        </div>
                    )}
                </div>

                <div className="px-5 py-4 text-sm text-zinc-800">
                    {loading && (
                        <div className="rounded border border-zinc-200 p-4 text-center text-zinc-500">
                            Loading trust summary…
                        </div>
                    )}
                    {error && (
                        <div className="rounded border border-red-200 bg-red-50 p-4 text-red-800">
                            Failed to load: {error}
                        </div>
                    )}
                    {data && (
                        <>
                            {/* Query echo + model + class */}
                            <div className="rounded border border-zinc-200 bg-zinc-50 p-3">
                                <div className="text-xs uppercase tracking-wide text-zinc-500">Query</div>
                                <div className="mt-1 text-sm italic">"{data.query_text}"</div>
                                <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-zinc-600">
                                    <div><span className="font-medium">Class:</span> {data.query_class}</div>
                                    <div><span className="font-medium">Model:</span> {data.model_name ?? '—'}</div>
                                    <div><span className="font-medium">Lifecycle:</span> {data.citation_lifecycle_state ?? '—'}</div>
                                    <div><span className="font-medium">Mode:</span> {data.citation_mode ?? '—'}</div>
                                </div>
                            </div>

                            {/* Sources rollup */}
                            <SectionHeading>Sources ({data.citations.total})</SectionHeading>
                            {data.citations.per_kind_state.length === 0 ? (
                                <div className="text-xs text-zinc-500">No citations attached.</div>
                            ) : (
                                <table className="mt-2 w-full text-xs">
                                    <thead>
                                        <tr className="border-b border-zinc-200 text-left text-zinc-500">
                                            <th className="py-1 pr-2">Store</th>
                                            <th className="py-1 pr-2">State</th>
                                            <th className="py-1 text-right">Count</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {data.citations.per_kind_state.map((c, i) => (
                                            <tr key={i} className="border-b border-zinc-100">
                                                <td className="py-1 pr-2 font-mono">{c.source_store}</td>
                                                <td className="py-1 pr-2">
                                                    <span className={c.state === 'accepted' ? 'text-emerald-700' : 'text-red-700'}>
                                                        {c.state}
                                                    </span>
                                                </td>
                                                <td className="py-1 text-right">{c.n}</td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            )}

                            {/* Evidence drill-in */}
                            <SectionHeading>Evidence ({data.sources.length})</SectionHeading>
                            {data.sources.length === 0 ? (
                                <div className="text-xs text-zinc-500">No source chunks to drill in.</div>
                            ) : (
                                <ul className="mt-2 space-y-1.5">
                                    {data.sources.map((s) => (
                                        <li
                                            key={s.citation_id}
                                            className="rounded border border-zinc-200 p-2 hover:bg-zinc-50"
                                        >
                                            <div className="flex items-center justify-between text-xs">
                                                <span className="font-mono text-zinc-600">{s.source_store}</span>
                                                <span
                                                    className={s.state === 'accepted' ? 'text-emerald-700' : 'text-red-700'}
                                                >
                                                    {s.state} · {(s.confidence * 100).toFixed(0)}%
                                                </span>
                                            </div>
                                            <div className="mt-1 text-sm">{s.marker_text || <span className="text-zinc-400">(no marker)</span>}</div>
                                            {s.rejection_reason && (
                                                <div className="mt-1 text-xs text-red-700">
                                                    Rejected: {s.rejection_reason}
                                                </div>
                                            )}
                                            {onOpenEvidence && s.evidence_id && (
                                                <button
                                                    type="button"
                                                    onClick={() => onOpenEvidence(s.evidence_id)}
                                                    className="mt-1 text-xs text-indigo-600 hover:underline"
                                                >
                                                    Open evidence →
                                                </button>
                                            )}
                                        </li>
                                    ))}
                                </ul>
                            )}

                            {/* Retrieval stages */}
                            <SectionHeading>Retrieval pipeline</SectionHeading>
                            {data.retrieval.per_stage.length === 0 ? (
                                <div className="text-xs text-zinc-500">No retrieval items recorded.</div>
                            ) : (
                                <table className="mt-2 w-full text-xs">
                                    <thead>
                                        <tr className="border-b border-zinc-200 text-left text-zinc-500">
                                            <th className="py-1 pr-2">Stage</th>
                                            <th className="py-1 text-right">Items</th>
                                            <th className="py-1 text-right">In ctx</th>
                                            <th className="py-1 text-right">Cited</th>
                                            <th className="py-1 text-right">Rerank μ</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {data.retrieval.per_stage.map((r, i) => (
                                            <tr key={i} className="border-b border-zinc-100">
                                                <td className="py-1 pr-2 font-mono">{r.stage}</td>
                                                <td className="py-1 text-right">{r.n}</td>
                                                <td className="py-1 text-right">{r.included}</td>
                                                <td className="py-1 text-right">{r.used_in_citation}</td>
                                                <td className="py-1 text-right">{r.avg_reranker?.toFixed(3) ?? '—'}</td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            )}

                            {/* Missing data */}
                            <SectionHeading>Missing or partial data</SectionHeading>
                            {data.missing_data.length === 0 ? (
                                <div className="text-xs text-emerald-700">✓ No known gaps.</div>
                            ) : (
                                <ul className="mt-2 space-y-1 text-xs">
                                    {data.missing_data.map((m, i) => (
                                        <li key={i} className="rounded bg-amber-50 px-2 py-1 text-amber-800">
                                            ⚠ {m}
                                        </li>
                                    ))}
                                </ul>
                            )}

                            {/* Conflicts */}
                            <SectionHeading>Conflicts</SectionHeading>
                            <div className="text-xs text-zinc-600">
                                {data.conflicts.count > 0
                                    ? `${data.conflicts.count} conflicts found — see Conflicts Resolver`
                                    : 'No conflicting evidence flagged.'}
                            </div>

                            {/* Assumptions (v2 — empty for now) */}
                            <SectionHeading>Assumptions</SectionHeading>
                            <div className="text-xs text-zinc-500">
                                {data.assumptions.length === 0
                                    ? '(LLM did not record explicit prior assumptions for this answer.)'
                                    : data.assumptions.join('; ')}
                            </div>

                            {/* Provenance */}
                            <SectionHeading>Provenance</SectionHeading>
                            <div className="text-xs text-zinc-600">
                                Run ID <code className="text-zinc-800">{data.answer_run_id}</code>
                                {data.created_at && (
                                    <span className="ml-2">· {new Date(data.created_at).toISOString().slice(0, 19).replace('T', ' ')} UTC</span>
                                )}
                                <span className="ml-2">· data v{data.data_version}</span>
                            </div>

                            {/* Feedback */}
                            {data.feedback.length > 0 && (
                                <>
                                    <SectionHeading>Recent feedback ({data.feedback.length})</SectionHeading>
                                    <ul className="mt-2 space-y-1 text-xs">
                                        {data.feedback.map((f, i) => (
                                            <li key={i} className="rounded border border-zinc-200 p-2">
                                                <span className={f.polarity === 'positive' ? 'text-emerald-700' : 'text-red-700'}>
                                                    {f.polarity}
                                                </span>
                                                {f.category && <span className="ml-2 text-zinc-500">[{f.category}]</span>}
                                                {f.note && <div className="mt-1 text-zinc-700">{f.note}</div>}
                                            </li>
                                        ))}
                                    </ul>
                                </>
                            )}
                        </>
                    )}
                </div>
            </aside>
        </div>
    );
}
