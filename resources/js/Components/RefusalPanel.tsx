/**
 * RefusalPanel — §10u Refusal & Uncertainty UX (chat-adjacent slice, built
 * 2026-09-24).
 *
 * Renders below an assistant bubble in place of the old plain-text error
 * footnote when the turn ended as either:
 *
 *   - a `failed` SSE frame — `event.error` (user-facing message) +
 *     `event.code` (a value of FastAPI's `ErrorCode` enum, e.g. `TIMEOUT`,
 *     `LLM_UNAVAILABLE`, `QUOTA_EXCEEDED` — see
 *     `src/fastapi/app/agent/errors.py::classify_error`), or
 *   - a `completed` SSE frame carrying `refusal_payload`
 *     (`GeoRAGResponse.refusal_payload` — always stamped on the Layer 1
 *     retrieval-gate refusal, `reason_code` `insufficient_evidence`, since
 *     CHAT-10; also by Plan §4b Stage 2 terminal strategies, gated behind
 *     `REPAIR_LOOP_TERMINAL_ENABLED`, where `reason_code` is a `GuardErrorCode`
 *     value such as `MISSING_ASSAY_UNITS`, `AMBIGUOUS_HOLE_ID`,
 *     `CONFLICTING_SOURCES`; `guard_codes` lists every code that fired).
 *
 * Deliberately non-hedging per §10u: "Insufficient evidence to answer this
 * question from the current corpus." is the fixed headline for a refusal
 * whose reason is `insufficient_evidence` (case-insensitive) or whose reason
 * is absent — the backend's own `message` renders as supporting detail
 * underneath, not as a replacement for it. A refusal for any OTHER reason
 * (an ambiguous hole id, conflicting sources, missing assay units ...) is
 * not an evidence shortfall, and saying so would send the reader looking
 * for data that is not missing, so it gets a neutral "Answer withheld"
 * heading over the backend's own message. The `failed` variant has no
 * fixed headline either (a timeout or a quota ceiling is not "insufficient
 * evidence"), so it shows the backend's own message under a neutral
 * "Query failed" heading.
 *
 * `code` is humanised from SCREAMING_SNAKE_CASE to Title Case rather than
 * looked up in an exhaustive label table: the three enums this prop can
 * carry (FastAPI's `ErrorCode`, `GuardErrorCode` via
 * `refusal_payload.reason_code`, and `silver.answer_runs`'
 * `RefusalReasonCode`) evolve independently, and a hardcoded label table
 * would silently go stale the next time one of them gains a value.
 *
 * `guardCodes` are internal guard identifiers. They are not shown to the
 * user (2026-10-04); they ride on `data-guard-codes` so support can still
 * read them from the DOM.
 */

export function humanizeCode(code: string): string {
    return code
        .split('_')
        .filter(Boolean)
        .map((word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
        .join(' ');
}

interface Props {
    variant: 'refusal' | 'failed';
    message: string;
    code?: string | null;
    guardCodes?: string[] | null;
}

export default function RefusalPanel({ variant, message, code, guardCodes }: Props) {
    if (!message) return null;

    const tone = 'var(--warn, #d97706)';
    const insufficientEvidence =
        variant === 'refusal' && (!code || code.trim().toLowerCase() === 'insufficient_evidence');
    const heading =
        variant === 'failed'
            ? 'Query failed'
            : insufficientEvidence
              ? 'Refused — insufficient evidence'
              : 'Answer withheld';

    return (
        <div
            data-testid="refusal-panel"
            data-variant={variant}
            data-guard-codes={guardCodes && guardCodes.length > 0 ? guardCodes.join(',') : undefined}
            role="alert"
            className="mt-2 rounded-md border px-3 py-2.5 text-xs leading-relaxed"
            style={{
                borderColor: tone,
                background: 'color-mix(in oklch, ' + tone + ' 8%, transparent)',
            }}
        >
            <div
                className="text-[10px] font-mono uppercase tracking-wider mb-1"
                style={{ color: tone }}
            >
                {heading}
            </div>
            {insufficientEvidence && (
                <div className="mb-1" style={{ color: 'var(--fg-1)' }}>
                    Insufficient evidence to answer this question from the current corpus.
                </div>
            )}
            <div style={{ color: 'var(--fg-2)' }}>{message}</div>
            {code && (
                <div className="mt-1.5 font-mono text-[10px]" style={{ color: 'var(--fg-3)' }}>
                    Reason: {humanizeCode(code)}
                </div>
            )}
        </div>
    );
}
