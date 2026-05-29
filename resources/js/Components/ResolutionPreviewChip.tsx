/**
 * ResolutionPreviewChip — plan §3e multi-turn resolution surface.
 *
 * Renders below an assistant message when the backend's resolve_node
 * rewrote the user's query (e.g. "what are ITS top assays?" →
 * "what are PLS-22-08's top assays?"). The chip shows both the
 * original and the rewritten form so the user can verify the
 * agent's interpretation.
 *
 * Renders nothing when:
 *   - `resolution` is null / undefined (resolve_node didn't fire)
 *   - `original_query === rewritten_query` (no rewrite happened)
 *
 * Confidence pill colour:
 *   high (≥ 0.85) — neutral green
 *   medium (0.6–0.85) — amber (still rendered; user should verify)
 *   low (< 0.6) — red (ambiguous; user should confirm)
 */

interface ResolutionStep {
    kind: string;
    original_phrase: string;
    resolved_to: string;
    source_turn_index?: number;
    confidence?: number;
    [k: string]: unknown;
}

interface ResolutionShape {
    original_query?: string;
    rewritten_query?: string;
    trace?: ResolutionStep[];
    overall_confidence?: number;
}

interface Props {
    resolution: Record<string, unknown> | null | undefined;
}

const baseChipClass =
    'inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-[10px] font-mono uppercase tracking-wider';

export default function ResolutionPreviewChip({ resolution }: Props) {
    if (!resolution) return null;
    const typed = resolution as ResolutionShape;
    const original = typed.original_query;
    const rewritten = typed.rewritten_query;
    if (!original || !rewritten || original === rewritten) return null;

    const confidence = typeof typed.overall_confidence === 'number'
        ? typed.overall_confidence
        : null;

    let tone: 'high' | 'medium' | 'low' | null = null;
    if (confidence !== null) {
        if (confidence >= 0.85) tone = 'high';
        else if (confidence >= 0.6) tone = 'medium';
        else tone = 'low';
    }

    const toneStyle =
        tone === 'low'
            ? { background: 'var(--bg-error, rgba(239, 68, 68, 0.12))', color: 'var(--error, #ef4444)' }
            : tone === 'medium'
              ? { background: 'var(--bg-warn, rgba(217, 119, 6, 0.12))', color: 'var(--warn, #d97706)' }
              : { background: 'var(--bg-3, rgba(120, 120, 120, 0.1))', color: 'var(--fg-3, #888)' };

    return (
        <div
            data-resolution-chip
            className="mt-2 rounded-md border px-3 py-2 text-xs"
            style={{
                borderColor: 'var(--border-2, rgba(120, 120, 120, 0.25))',
                background: 'var(--bg-2, rgba(120, 120, 120, 0.04))',
            }}
        >
            <div className="flex flex-wrap items-baseline gap-2">
                <span
                    className={baseChipClass}
                    style={{ background: 'var(--bg-3, rgba(120, 120, 120, 0.1))', color: 'var(--fg-3, #888)' }}
                >
                    Interpreted as
                </span>
                <span className="font-mono" style={{ color: 'var(--fg-1)' }}>
                    {rewritten}
                </span>
                {tone && (
                    <span
                        className={baseChipClass}
                        style={toneStyle}
                        title={
                            confidence !== null
                                ? `Resolver confidence: ${(confidence * 100).toFixed(0)}%`
                                : undefined
                        }
                    >
                        {tone === 'high' ? 'high' : tone === 'medium' ? 'medium' : 'low'}
                    </span>
                )}
            </div>
            <div
                className="mt-1 text-[10px] italic"
                style={{ color: 'var(--fg-3, #888)' }}
            >
                from your message: {original}
            </div>
        </div>
    );
}
