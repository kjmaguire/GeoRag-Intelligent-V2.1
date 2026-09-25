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
 *     (`GeoRAGResponse.refusal_payload` — Plan §4b Stage 2, gated behind
 *     `REPAIR_LOOP_TERMINAL_ENABLED`; `reason_code` is a `GuardErrorCode`
 *     value such as `MISSING_ASSAY_UNITS`, `AMBIGUOUS_HOLE_ID`,
 *     `CONFLICTING_SOURCES`; `guard_codes` lists every code that fired).
 *
 * Deliberately non-hedging per §10u: "Insufficient evidence to answer this
 * question from the current corpus." is the fixed headline for the refusal
 * variant — the backend's own `message` renders as supporting detail
 * underneath, not as a replacement for it. The `failed` variant has no
 * fixed headline (a timeout or a quota ceiling is not "insufficient
 * evidence"), so it shows the backend's own message under a neutral
 * "This query failed." heading.
 *
 * `code` is humanised from SCREAMING_SNAKE_CASE to Title Case rather than
 * looked up in an exhaustive label table: the three enums this prop can
 * carry (FastAPI's `ErrorCode`, `GuardErrorCode` via
 * `refusal_payload.reason_code`, and `silver.answer_runs`'
 * `RefusalReasonCode`) evolve independently, and a hardcoded label table
 * would silently go stale the next time one of them gains a value.
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

    return (
        <div
            data-testid="refusal-panel"
            data-variant={variant}
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
                {variant === 'refusal' ? 'Refused — insufficient evidence' : 'Query failed'}
            </div>
            {variant === 'refusal' && (
                <div className="mb-1" style={{ color: 'var(--fg-1)' }}>
                    Insufficient evidence to answer this question from the current corpus.
                </div>
            )}
            <div style={{ color: 'var(--fg-2)' }}>{message}</div>
            {code && (
                <div className="mt-1.5 font-mono text-[10px]" style={{ color: 'var(--fg-3)' }}>
                    Reason: {humanizeCode(code)}
                    {guardCodes && guardCodes.length > 0 && ` (guards: ${guardCodes.join(', ')})`}
                </div>
            )}
        </div>
    );
}
