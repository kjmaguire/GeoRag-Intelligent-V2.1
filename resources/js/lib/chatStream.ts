/**
 * Pure helpers for the Foundry chat stream (resources/js/Pages/Foundry/Chat.tsx).
 *
 * Kept out of the page so the rules the audit found broken can be tested
 * without a Reverb socket:
 *   - deltas render in token_seq order and a re-delivered frame is dropped
 *     (CHAT-15);
 *   - a `status` heartbeat keeps the watchdog alive without replacing the
 *     phase text (CHAT-6);
 *   - a completed answer with no citations is flagged, never shown as a
 *     normal answer (CHAT-16, CLAUDE.md hard rule 4);
 *   - what a persisted message carries so a reopened thread keeps its
 *     verdicts (CHAT-4, CHAT-9);
 *   - the Workspace copilot's `?prompt=` hand-off (FE-4).
 *
 * SSE vocabulary: status · bind · delta · citation · completed · failed
 * (the same six names as src/fastapi/app/routers/queries.py and
 * app/Jobs/StreamQueryFromFastApi.php; SseVocabularyContractTest pins it).
 */

export const SSE_VOCABULARY = ['status', 'bind', 'delta', 'citation', 'completed', 'failed'] as const;
export type SseEventName = (typeof SSE_VOCABULARY)[number];

export type ValidationState = 'clean' | 'unverified' | 'flagged';

/**
 * The verdict a `completed` frame carries. Anything other than an explicit
 * `clean`/`flagged` is `unverified`: the safe default is the one that does
 * not claim a check happened.
 */
export function normaliseValidationState(raw: unknown): ValidationState {
    if (raw === 'clean') return 'clean';
    if (raw === 'flagged') return 'flagged';
    return 'unverified';
}

/**
 * CHAT-16 — a completed, non-refused answer with zero citations is an
 * upstream defect (every RAG claim must carry a source_chunk_id). It must
 * not render like a normal, checked answer.
 */
export function isUncitedAnswer(citations: unknown, refusalPayload: unknown): boolean {
    if (refusalPayload) return false;
    return !Array.isArray(citations) || citations.length === 0;
}

/** CHAT-6 — FastAPI's keep-alive rides `status` with heartbeat=true. */
export function isHeartbeat(event: Record<string, unknown>): boolean {
    return event.event === 'status' && event.heartbeat === true;
}

/**
 * CHAT-15 — assembles streamed tokens by token_seq, not arrival order, and
 * drops a frame whose event_id was already applied. Reverb runs two tasks
 * behind a Redis backplane, so consecutive broadcasts can land out of order
 * and a reconnect can re-deliver one.
 *
 * Gaps are NOT waited for: FastAPI skips a token_seq when a whole chunk was
 * a Cohere sentinel, so "render the contiguous prefix" would stall forever.
 * Whatever has arrived is rendered in seq order. Tokens without a seq keep
 * arrival order after every sequenced token.
 */
export interface DeltaBuffer {
    /** Adds a token; returns the full text in order, or null for a duplicate. */
    add(token: string, tokenSeq: unknown, eventId: unknown): string | null;
    text(): string;
}

export function createDeltaBuffer(): DeltaBuffer {
    const seen = new Set<string>();
    const parts: Array<{ seq: number; arrival: number; token: string }> = [];
    let arrival = 0;

    const text = () => parts.map((p) => p.token).join('');

    return {
        add(token, tokenSeq, eventId) {
            if (typeof eventId === 'string' && eventId !== '') {
                if (seen.has(eventId)) return null;
                seen.add(eventId);
            }
            const seq = typeof tokenSeq === 'number' && Number.isFinite(tokenSeq) ? tokenSeq : Number.POSITIVE_INFINITY;
            const entry = { seq, arrival: arrival++, token };
            // Insert after the last entry that sorts at or before this one
            // (stable for equal seqs; typical arrival is in order, so this
            // walks one step from the end).
            let i = parts.length;
            while (i > 0 && (parts[i - 1].seq > entry.seq)) i--;
            parts.splice(i, 0, entry);
            return text();
        },
        text,
    };
}

/** FE-4 — the Workspace copilot dock hands its question over as `?prompt=`. */
export function readPromptParam(search: string): string {
    try {
        return (new URLSearchParams(search).get('prompt') ?? '').trim().slice(0, 4000);
    } catch {
        return '';
    }
}

export interface PersistableMessage {
    role: string;
    content: string;
    citations: unknown[];
    confidence: number | null;
    validationState?: ValidationState | null;
    answer_run_id: string | null;
    error?: string | null;
    errorCode?: string | null;
    refusalPayload?: Record<string, unknown> | null;
    citationsMissing?: boolean | null;
}

/**
 * The PUT /api/v1/conversations/{id} shape for one message.
 *
 * Carries every verdict the page renders (CHAT-9) — Foundry ChatController
 * maps the same keys back — and keeps an assistant turn that failed before
 * any text as '' plus its error (CHAT-4), which the server now accepts.
 */
export function toPersistedMessage(m: PersistableMessage): { role: string; content: string; metadata: Record<string, unknown> } {
    return {
        role: m.role,
        content: m.content ?? '',
        metadata: {
            citations: m.citations,
            confidence: m.confidence,
            validation_state: m.validationState ?? null,
            answer_run_id: m.answer_run_id,
            refusal_payload: m.refusalPayload ?? null,
            error: m.error ?? null,
            error_code: m.errorCode ?? null,
            citations_missing: m.citationsMissing ?? null,
        },
    };
}
