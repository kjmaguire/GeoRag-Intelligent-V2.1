import { useEffect, useState } from 'react';
import { Link } from '@inertiajs/react';
import { Sheet, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle } from '@/Components/ui/sheet';

/**
 * EvidenceInspector — §10s Evidence Inspector (chat-adjacent slice, built
 * 2026-09-24).
 *
 * Opens as a slide-in panel when a citation chip is clicked. Reuses the
 * existing `GET /api/v1/citations/resolve` endpoint (already called inline
 * by `MessageBubble` in `Pages/Foundry/Chat.tsx`) rather than the unwired
 * `GET /api/v1/evidence/{evidence_id}` contract described in §10s — that
 * route exists server-side (`App\Http\Controllers\Api\V1\EvidenceController`)
 * but is keyed by `evidence_id` (`silver.evidence_items`), a field our
 * streamed `Citation` objects don't carry; `citations/resolve` is keyed by
 * the `source_chunk_id` every citation already has, is already exercised
 * in production, and returns the same shape §10s asks for: exact passage
 * text, document title, section, and a generic `metadata` bag. Only an
 * allow-listed handful of its keys are shown (see evidenceFacts); the bag
 * itself carries ids and raw rows. `support_type` is not rendered — §10s's own corrected note
 * says the field does not exist anywhere in the schema.
 *
 * Renders nothing (Sheet stays closed) when `citation` is null.
 */

interface EvidenceCitation {
    citation_id: string;
    citation_type: string;
    source_chunk_id: string;
    document_title?: string;
    relevance_score?: number;
}

interface ResolvedEvidence {
    text: string;
    source_type: string;
    title?: string;
    section_title?: string;
    section_number?: string | null;
    metadata?: Record<string, unknown>;
}

interface Props {
    citation: EvidenceCitation | null;
    open: boolean;
    onOpenChange: (open: boolean) => void;
    projectSlug: string;
    // The answer this citation belongs to. Feedback is recorded against an
    // answer run, so without one (a turn that errored before a run was
    // persisted) there is nowhere to send a report and the button is not
    // offered.
    answerRunId?: string | null;
    // Wired by the caller to pre-fill FeedbackControls' thumbs-down
    // taxonomy to 'citation_issue' (§10p) — the inspector itself has no
    // opinion on how feedback is collected, it just raises the intent.
    onReportIssue?: (citation: EvidenceCitation) => void;
}

/** One labelled fact shown under the passage text. */
interface EvidenceFact {
    label: string;
    value: string;
}

function textValue(v: unknown): string | null {
    if (typeof v === 'string') return v.trim() === '' ? null : v.trim();
    if (typeof v === 'number' && Number.isFinite(v)) return String(v);
    return null;
}

function depthValue(v: unknown): number | null {
    const n = typeof v === 'string' && v.trim() !== '' ? Number(v) : v;
    return typeof n === 'number' && Number.isFinite(n) ? n : null;
}

/**
 * The facts a geologist reads off a source: where in the document, which
 * hole and interval, when it was filed, how sure the match is.
 *
 * Deliberately an allow-list. The resolve endpoint's `metadata` is a raw bag
 * that differs per source type (a whole collar row, an assay row with lab and
 * certificate fields, internal ids), and printing it key by key put ids and
 * database columns in front of the reader. A key that is not listed here is
 * not shown; add it when a reader has a reason to want it.
 */
function evidenceFacts(metadata: Record<string, unknown> | undefined): EvidenceFact[] {
    if (!metadata) return [];
    const facts: EvidenceFact[] = [];
    const push = (label: string, value: string | null) => {
        if (value !== null) facts.push({ label, value });
    };

    push('Document', textValue(metadata.document_title));
    push('Section', textValue(metadata.section));
    push('Page', textValue(metadata.page) ?? textValue(metadata.page_number) ?? textValue(metadata.page_first));
    push('Hole', textValue(metadata.hole_id));

    const from = depthValue(metadata.from_depth);
    const to = depthValue(metadata.to_depth);
    if (from !== null && to !== null) push('Depth', `${from}–${to} m`);

    const filed = textValue(metadata.filing_date);
    push('Report date', filed ? filed.slice(0, 10) : null);

    const confidence = depthValue(metadata.confidence);
    if (confidence !== null) {
        push(
            'Confidence',
            confidence >= 0 && confidence <= 1 ? `${Math.round(confidence * 100)}%` : String(confidence),
        );
    }
    return facts;
}

export default function EvidenceInspector({
    citation,
    open,
    onOpenChange,
    projectSlug,
    answerRunId,
    onReportIssue,
}: Props) {
    const [resolved, setResolved] = useState<ResolvedEvidence | 'loading' | 'error' | null>(null);

    useEffect(() => {
        if (!open || !citation?.source_chunk_id) {
            return;
        }
        let cancelled = false;
        setResolved('loading');
        fetch(`/api/v1/citations/resolve?source_chunk_id=${encodeURIComponent(citation.source_chunk_id)}`, {
            credentials: 'same-origin',
            headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        })
            .then((resp) => {
                if (!resp.ok) throw new Error(`resolve failed (${resp.status})`);
                return resp.json();
            })
            .then((data: ResolvedEvidence) => {
                if (!cancelled) setResolved(data);
            })
            .catch(() => {
                if (!cancelled) setResolved('error');
            });
        return () => {
            cancelled = true;
        };
    }, [open, citation?.source_chunk_id]);

    const facts = resolved && resolved !== 'loading' && resolved !== 'error' ? evidenceFacts(resolved.metadata) : [];

    const reportId =
        resolved && resolved !== 'loading' && resolved !== 'error' && typeof resolved.metadata?.report_id === 'string'
            ? (resolved.metadata.report_id as string)
            : null;
    const sectionParam =
        resolved &&
        resolved !== 'loading' &&
        resolved !== 'error' &&
        resolved.section_number &&
        resolved.section_number !== 'unknown'
            ? resolved.section_number
            : null;

    return (
        <Sheet open={open} onOpenChange={onOpenChange}>
            <SheetContent data-testid="evidence-inspector" className="overflow-y-auto">
                <SheetHeader>
                    <SheetTitle>
                        {(resolved && resolved !== 'loading' && resolved !== 'error' && resolved.title) ||
                            citation?.document_title ||
                            'Source evidence'}
                    </SheetTitle>
                    <SheetDescription>
                        {citation?.citation_type ?? 'source'}
                        {resolved && resolved !== 'loading' && resolved !== 'error' && resolved.section_title
                            ? ` · ${resolved.section_title}`
                            : ''}
                        {typeof citation?.relevance_score === 'number'
                            ? ` · ${(citation.relevance_score * 100).toFixed(0)}% relevance`
                            : ''}
                    </SheetDescription>
                </SheetHeader>

                <div className="px-4 text-sm space-y-4">
                    {resolved === 'loading' && (
                        <p className="text-muted-foreground" data-testid="evidence-inspector-loading">
                            Loading source…
                        </p>
                    )}
                    {resolved === 'error' && (
                        <p className="text-destructive" role="alert" data-testid="evidence-inspector-error">
                            Could not load this source.
                        </p>
                    )}
                    {resolved && resolved !== 'loading' && resolved !== 'error' && (
                        <>
                            <div className="whitespace-pre-wrap leading-relaxed" data-testid="evidence-inspector-text">
                                {resolved.text}
                            </div>
                            {facts.length > 0 && (
                                <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs text-muted-foreground">
                                    {facts.map((fact) => (
                                        <div key={fact.label} className="contents">
                                            <dt className="font-mono uppercase tracking-wider">{fact.label}</dt>
                                            <dd>{fact.value}</dd>
                                        </div>
                                    ))}
                                </dl>
                            )}
                            {reportId && (
                                <Link
                                    href={`/projects/${projectSlug}/reports/${reportId}${sectionParam ? `?section=${encodeURIComponent(sectionParam)}` : ''}`}
                                    className="inline-flex items-center gap-1 font-mono text-[11px] uppercase tracking-wider underline"
                                >
                                    Open in Reader →
                                </Link>
                            )}
                        </>
                    )}
                </div>

                <SheetFooter>
                    {citation && answerRunId && onReportIssue && (
                        <button
                            type="button"
                            onClick={() => onReportIssue(citation)}
                            className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border self-start"
                        >
                            👎 Report citation issue
                        </button>
                    )}
                </SheetFooter>
            </SheetContent>
        </Sheet>
    );
}
