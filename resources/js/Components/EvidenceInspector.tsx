import { useEffect, useState } from 'react';
import { Link } from '@inertiajs/react';
import {
    Sheet,
    SheetContent,
    SheetDescription,
    SheetFooter,
    SheetHeader,
    SheetTitle,
} from '@/Components/ui/sheet';

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
 * text, document title, section, and a generic `metadata` bag (filing
 * date / company / commodity for reports; collar/hole ids for structured
 * sources). `support_type` is not rendered — §10s's own corrected note
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
    // Wired by the caller to pre-fill FeedbackControls' thumbs-down
    // taxonomy to 'citation_issue' (§10p) — the inspector itself has no
    // opinion on how feedback is collected, it just raises the intent.
    onReportIssue?: (citation: EvidenceCitation) => void;
}

// Keys already shown elsewhere in the panel (title / section) — skipped
// from the generic metadata dump so nothing renders twice.
const METADATA_LABEL_OVERRIDES: Record<string, string> = {
    filing_date: 'Filing date',
    report_id: 'Report ID',
    company: 'Company',
    commodity: 'Commodity',
    hole_id: 'Hole ID',
    collar_id: 'Collar ID',
};

function humanizeKey(key: string): string {
    return METADATA_LABEL_OVERRIDES[key] ?? key
        .split('_')
        .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
        .join(' ');
}

export default function EvidenceInspector({ citation, open, onOpenChange, projectSlug, onReportIssue }: Props) {
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

    const metadataEntries = (() => {
        if (!resolved || resolved === 'loading' || resolved === 'error' || !resolved.metadata) return [];
        return Object.entries(resolved.metadata).filter(([, v]) => v !== null && v !== undefined && v !== '');
    })();

    const reportId =
        resolved && resolved !== 'loading' && resolved !== 'error' && typeof resolved.metadata?.report_id === 'string'
            ? (resolved.metadata.report_id as string)
            : null;
    const sectionParam =
        resolved && resolved !== 'loading' && resolved !== 'error' && resolved.section_number && resolved.section_number !== 'unknown'
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
                            {metadataEntries.length > 0 && (
                                <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs text-muted-foreground">
                                    {metadataEntries.map(([key, value]) => (
                                        <div key={key} className="contents">
                                            <dt className="font-mono uppercase tracking-wider">{humanizeKey(key)}</dt>
                                            <dd>{String(value)}</dd>
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
                    {citation && onReportIssue && (
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
