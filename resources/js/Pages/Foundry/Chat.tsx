import { memo, useCallback, useEffect, useRef, useState, useMemo } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { Pill, EmptyState, BrandDiamond } from '@/Components/Foundry/primitives';
import EvidencePacketBadge from '@/Components/EvidencePacketBadge';
import InlineViz from '@/Components/InlineViz';
import ResolutionPreviewChip from '@/Components/ResolutionPreviewChip';
import RefusalPanel from '@/Components/RefusalPanel';
import EvidenceInspector from '@/Components/EvidenceInspector';
import FeedbackControls from '@/Components/FeedbackControls';
import CitationPGEODetail from '@/Components/PublicGeoscience/CitationPGEODetail';
import type { Citation as SharedCitation } from '@/types';
import {
    createDeltaBuffer,
    isHeartbeat,
    isUncitedAnswer,
    normaliseValidationState,
    readPromptParam,
    toPersistedMessage,
    type ValidationState,
} from '@/lib/chatStream';
import { formatTime, formatWhen } from '@/lib/time';
import {
    ContextEnvelopeForm,
    EMPTY_ENVELOPE,
    applySmartDefaults,
    type ContextEnvelope,
} from '@/Components/Foundry/ContextEnvelopeForm';

/**
 * Foundry/Chat — project-scoped streaming RAG chat surface.
 *
 * Hits the existing `/api/v1/queries` two-phase subscribe-ACK pipeline:
 *   1. POST /api/v1/queries { query, project_id } → { query_id, channel }
 *   2. Echo.channel(channel).listen('.QueryStreamEvent', handler)
 *   3. POST /api/v1/queries/{id}/start (dispatches the Horizon job)
 *   4. Stream events — SSE vocabulary: status · bind · delta · citation · completed · failed
 *      (identical in src/fastapi/app/routers/queries.py and
 *      app/Jobs/StreamQueryFromFastApi.php; SseVocabularyContractTest pins
 *      all three). `bind` is additive and not rendered yet; a `status`
 *      with heartbeat=true only keeps the idle watchdog alive.
 *
 * Recovery (CHAT-8): if the terminal frame is lost — reconnect, oversized
 * frame, idle watchdog — the page asks GET /api/v1/queries/{id}/result for
 * the finalised answer before declaring failure. Stop calls
 * POST /api/v1/queries/{id}/cancel so the job and the LLM run actually stop.
 *
 * On `completed`, persists the full conversation via PUT
 * /api/v1/conversations/{uuid} so the threads rail picks it up on next
 * page load.
 *
 * Not yet ported from legacy Pages/Chat.tsx:
 *   - TrustInspector side panel
 *   - Conflict-detection + freshness rendering
 *   - Follow-up suggestion rendering (backend doesn't generate these yet —
 *     see §10q as-built note, nothing to port)
 *   - WS-01 reconnect / event-dedup layer
 * These all live in the backend already (their payload fields land on
 * the completed event), so they can be layered in without server work.
 *
 * Built 2026-09-24 (chat-adjacent design-only pieces, §10s/§10p/§10u):
 *   - RefusalPanel — renders a typed refusal/failure panel instead of a
 *     generic error footnote, off the `failed` frame's `error`/`code` or
 *     the `completed` frame's `refusal_payload`.
 *   - EvidenceInspector — a Sheet-style panel triggered from a citation
 *     chip, built on the existing GET /api/v1/citations/resolve endpoint
 *     already called inline below (no new backend route needed).
 *   - FeedbackControls — 👍/👎 + optional note on a settled assistant
 *     answer, proxied through the new POST
 *     /api/v1/answer_runs/{id}/feedback Laravel route to FastAPI's
 *     existing silver.message_feedback writer.
 */

interface ChatThread { id: string; title: string; updated: string }
interface Citation {
    citation_id: string;
    citation_type: string;
    source_chunk_id: string;
    document_title?: string;
    relevance_score?: number;
    // Public Geoscience extensions (plan §08) — present only on PGEO
    // citations, needed to render CitationPGEODetail. Previously dropped by
    // the per-token streaming 'citation' handler below (only the 'completed'
    // event's bulk citations array carried them, via an `as Citation[]`
    // cast that bypassed this interface entirely) — CitationPGEODetail
    // existed, fully built and tested, with zero importers anywhere in the
    // app because nothing in Chat.tsx could type its way to rendering it.
    corpus?: 'internal_archive' | 'public_geo' | null;
    jurisdiction_code?: string | null;
    jurisdiction_name?: string | null;
    license_summary?: string | null;
    license_url?: string | null;
    source_url?: string | null;
    staleness_seconds?: number | null;
}
// ValidationState — what the §04i post-assembly guards concluded about an
// answer — lives in @/lib/chatStream. Deliberately separate from
// `confidence`, which is a retrieval-strength number: every structured tool
// contributes a flat 1.0 when it returned any rows, so `conf 0.95` means
// "PostGIS found some collars", not "this answer is right". Backend:
// GeoRAGResponse.validation_state.

interface ChatMessage {
    id: string;
    role: 'user' | 'assistant' | string;
    content: string;
    created_at: string;
    citations: Citation[];
    // RETRIEVAL strength, not answer quality — see GeoRAGResponse.confidence.
    // Every structured tool contributes a flat 1.0 when it returned any rows,
    // so this is 0.95 for any question asked of a project that has drilling.
    confidence: number | null;
    // What the §04i guards concluded about the answer itself. Rendered as the
    // pill's tone; `confidence` alone cannot carry it.
    validationState?: ValidationState | null;
    answer_run_id: string | null;
    status?: string | null;
    error?: string | null;
    // Built 2026-09-24 — typed error code off the `failed`/`error` SSE
    // frame (app/agent/errors.py's ErrorCode enum: TIMEOUT,
    // LLM_UNAVAILABLE, QUOTA_EXCEEDED, ...). Null for client-side errors
    // (network failure before the stream opened) that never carried one.
    errorCode?: string | null;
    // Built 2026-09-24 — structured refusal payload off the `completed`
    // frame (GeoRAGResponse.refusal_payload, Plan §4b Stage 2, gated
    // behind REPAIR_LOOP_TERMINAL_ENABLED). Shape:
    // {type, reason_code, strategy, message, candidates, guard_codes}.
    // Null on every committed (non-refused) answer.
    refusalPayload?: {
        type?: string;
        reason_code?: string;
        strategy?: string;
        message?: string;
        candidates?: string[];
        guard_codes?: string[];
    } | null;
    isStreaming?: boolean;
    // CHAT-16 — a completed, non-refused answer that arrived with zero
    // citations. Rendered with a visible "treat as unverified" warning.
    citationsMissing?: boolean | null;
    // CHAT-5 — the job slimmed an oversized `completed` frame and these
    // fields (e.g. viz_payload) were left out of it.
    truncatedFields?: string[] | null;
    // M2 P5 visualization payloads — backend emits these on the completed
    // SSE event (src/fastapi/app/agent/agentic_retrieval/nodes.py:_build_chat_card_payloads).
    // Both null until the completed handler captures them. InlineViz no-ops
    // when both are null/undefined.
    mapPayload?: Record<string, unknown> | null;
    vizPayload?: Record<string, unknown> | null;
    // Plan §3a/§3b — typed evidence packet. Backend stamps this on
    // GeoRAGResponse.evidence_packet in agentic_retrieval/nodes.py's
    // persist_node. Shape: { evidence: [{kind, ...}, ...], remaining_budget,
    // total_tokens, ... }. EvidencePacketBadge no-ops when null/empty.
    evidencePacket?: Record<string, unknown> | null;
    // Plan §3e — multi-turn resolution audit. Backend stamps when
    // resolve_node rewrote the query. Shape: {original_query,
    // rewritten_query, trace[], overall_confidence}. ResolutionChip
    // no-ops when null.
    multiTurnResolution?: Record<string, unknown> | null;
}

interface ChatPageProps {
    project: {
        project_id: string;
        project_name: string;
        slug: string;
        // Phase 3 / Step 3.2 — passed by Foundry/ChatController.show() so
        // the ContextEnvelopeForm can pre-populate smart defaults.
        crs_datum?: string | null;
        crs_epsg?: number | null;
        region?: string | null;
        commodity?: string | null;
    };
    threads: ChatThread[];
    active_thread_id: string | null;
    active_thread: { id: string; title: string } | null;
    messages: ChatMessage[];
    empty: boolean;
}

// Six suggestion chips covering the main intent classes. Neutral geology
// prompts; the commodity-specific ones are filled in from the project's own
// `commodity` when it has one and fall back to a commodity-free wording.
function suggestionChips(commodity?: string | null): Array<{ label: string; query: string }> {
    const c = commodity?.trim();
    return [
        { label: 'How many drill holes are in this project?', query: 'How many drill holes are in this project?' },
        { label: 'What is the deepest hole on this project?', query: 'What is the deepest drill hole on this project, and what is its total depth?' },
        { label: 'Summarise the lithology of the main zone', query: 'Summarise the lithology of the main zone on this project.' },
        { label: 'Which reports mention a resource estimate?', query: 'Which reports mention a resource estimate, and what do they report?' },
        c
            ? { label: `Compare mean ${c} grade across holes`, query: `Compare the mean ${c} grade across every hole in this project.` }
            : { label: 'Which holes have the best intervals?', query: 'Which holes have the best assay intervals in this project?' },
        { label: 'What deposit does this project host?', query: 'What deposit style does this project host and what is its geological setting?' },
    ];
}

// Crypto.randomUUID polyfill for older browsers.
function newUuid(): string {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
        return crypto.randomUUID();
    }
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0;
        return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
    });
}

function getCsrf(): string | null {
    return document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') ?? null;
}

/** Headers every same-origin JSON call from this page sends. */
function jsonHeaders(): Record<string, string> {
    const csrf = getCsrf();
    return {
        'Content-Type': 'application/json',
        Accept: 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
        ...(csrf ? { 'X-CSRF-TOKEN': csrf } : {}),
    };
}

/** The query a stream belongs to — what recovery and Stop address. */
interface ActiveQuery {
    queryId: string;
    assistantId: string;
    convoId: string;
}

/** pusher-js connection state_change payload. */
type ConnectionStateListener = (states: { previous: string; current: string }) => void;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const window: any;

// InlineViz re-renders (and can re-mount a Plotly/MapLibre canvas) on any
// parent render unless its props are referentially stable; they are, because
// the payloads are carried on the message object.
const MemoInlineViz = memo(InlineViz);

/** Distance from the bottom, in px, within which the transcript follows new text. */
const STICK_TO_BOTTOM_PX = 80;

const STOPPED_PARTIAL = 'Stopped by you. The text above is incomplete and unchecked.';
const STOPPED_EMPTY = 'Stopped by you before any answer text arrived.';

export default function FoundryChat({ project, threads, active_thread_id, active_thread, messages: initialMessages }: ChatPageProps) {
    // FE-4 — the Workspace copilot dock navigates here with ?prompt=; the
    // question is prefilled (not auto-sent) so the user sees and owns it.
    const [composer, setComposer] = useState(() => (typeof window === 'undefined' ? '' : readPromptParam(window.location.search)));
    // CHAT-4 — surfaced when a thread sync is rejected.
    const [persistError, setPersistError] = useState<string | null>(null);
    const [messages, setMessages] = useState<ChatMessage[]>(initialMessages);
    const [streaming, setStreaming] = useState(false);
    const [conversationId, setConversationId] = useState<string>(active_thread_id ?? '');
    // Mobile (< lg) thread rail — collapsed by default, toggled by the
    // hamburger button in the conversation header. Below `lg:` the rail
    // renders inline above the conversation (same "toggle reveals a block
    // below the header" convention as FoundryShell's own `sm:hidden`
    // hamburger drawer) rather than a fixed overlay.
    const [mobileThreadsOpen, setMobileThreadsOpen] = useState(false);

    const chips = useMemo(() => suggestionChips(project.commodity), [project.commodity]);

    // Phase 3 / Steps 3.2 + 3.3 — context envelope + Field/Office mode.
    // Mode is persisted per-user in localStorage; the 12 fields reset each
    // page load (per the plan's "any field left blank is submitted as
    // unspecified" rule, the geologist re-enters them per session).
    const persistedMode = useMemo<'field' | 'office'>(() => {
        if (typeof window === 'undefined') return 'office';
        const v = window.localStorage.getItem('georag_query_mode');
        return v === 'field' ? 'field' : 'office';
    }, []);
    const [envelope, setEnvelope] = useState<ContextEnvelope>(() =>
        applySmartDefaults({ ...EMPTY_ENVELOPE, mode: persistedMode }, project),
    );
    useEffect(() => {
        if (typeof window !== 'undefined') {
            window.localStorage.setItem('georag_query_mode', envelope.mode);
        }
    }, [envelope.mode]);

    // Build the JSON payload from the envelope. Blank string fields are
    // already null; empty arrays stay as-is. The Laravel validator accepts
    // null on every field per Phase 2.4's "unspecified" contract.
    const buildEnvelopePayload = (env: ContextEnvelope): Record<string, unknown> => ({
        area_of_interest: env.area_of_interest,
        crs_epsg: env.crs_epsg,
        depth_reference: env.depth_reference,
        scale_resolution: env.scale_resolution,
        stratigraphic_frame: env.stratigraphic_frame,
        specific_objects: env.specific_objects,
        data_sources: env.data_sources,
        qaqc_constraints: env.qaqc_constraints,
        units_and_detection_limits: env.units_and_detection_limits,
        reporting_code: env.reporting_code,
        decision_to_support: env.decision_to_support,
        desired_output_structure: env.desired_output_structure,
        mode: env.mode,
    });
    // Echo channel + name held in a ref so the Stop button can leave it
    // without re-binding handlers on every render.
    const echoRef = useRef<{ channel: { stopListening: (e: string) => void }; name: string } | null>(null);
    // The query currently streaming — what recovery (CHAT-8) and Stop
    // (CHAT-18) need to address the server about it.
    const activeQueryRef = useRef<ActiveQuery | null>(null);
    // The conversation id of the stream in flight, for applyFailed().
    const activeConvoIdRef = useRef<string>('');
    // pusher-js connection listener bound for the life of one stream, so a
    // reconnect can trigger recovery (CHAT-8). Held for unbinding.
    const connectionListenerRef = useRef<ConnectionStateListener | null>(null);
    const scrollerRef = useRef<HTMLDivElement | null>(null);
    // Whether the transcript is following the newest text. Cleared when the
    // reader scrolls up, so streaming never yanks them back down (item: forced
    // auto-scroll); set again on send and when they return to the bottom.
    const stickToBottomRef = useRef(true);
    // Streamed tokens are buffered and flushed to React state at most once per
    // animation frame, instead of one setMessages (and one re-render of the
    // whole transcript) per token.
    const flushFrameRef = useRef<number | null>(null);
    const pendingFlushRef = useRef<(() => void) | null>(null);
    // P0.2 — timeout watchdog, reworked 2026-08-11. The original version
    // armed a single 60s wall-clock timer at send and REPLACED the
    // assistant message content when it fired — on a pipeline whose
    // server-side budget is 270-300s, that destroyed fully-streamed
    // correct answers whose completed frame was late (observed live
    // when Reverb rejected oversized terminal frames). Now:
    //   - the timers are IDLE timers, re-armed on every frame received
    //     (status/delta/citation — including FastAPI's status heartbeat,
    //     sent every 15 s of silence, so a slow first token no longer
    //     looks like a dead channel: CHAT-6);
    //   - warn  @  30s idle — flip the status line to "still working";
    //   - fatal @ 120s idle — ask the server for the finalised answer
    //     first (CHAT-8); only if there is none, mark the message failed
    //     but PRESERVE any streamed content.
    // Timers are held in one ref keyed by the owning assistant id so a
    // thread switch or overlapping send can't fire a stale timer against
    // the wrong message.
    const watchdogRef = useRef<{
        assistantId: string;
        warn: ReturnType<typeof setTimeout>;
        fatal: ReturnType<typeof setTimeout>;
    } | null>(null);

    function clearWatchdog() {
        const w = watchdogRef.current;
        if (w) {
            clearTimeout(w.warn);
            clearTimeout(w.fatal);
            watchdogRef.current = null;
        }
    }

    /**
     * Apply a change to one message in BOTH the state and the ref snapshot.
     * Terminal handlers build their next state from messagesRef (see
     * applyCompleted), and the ref is only re-synced after a render, so a
     * state-only update made in the same tick would be invisible to them.
     * `patch` must be pure — it runs once against each.
     */
    function patchMessage(id: string, patch: (m: ChatMessage) => ChatMessage) {
        messagesRef.current = messagesRef.current.map((m) => (m.id === id ? patch(m) : m));
        setMessages((prev) => prev.map((m) => (m.id === id ? patch(m) : m)));
    }

    function cancelDeltaFlush() {
        if (flushFrameRef.current !== null && typeof cancelAnimationFrame === 'function') {
            cancelAnimationFrame(flushFrameRef.current);
        }
        flushFrameRef.current = null;
        pendingFlushRef.current = null;
    }

    /** Apply any buffered tokens now — before a terminal path reads the message. */
    function flushDeltasNow() {
        const pending = pendingFlushRef.current;
        cancelDeltaFlush();
        pending?.();
    }

    function scheduleDeltaFlush(apply: () => void) {
        pendingFlushRef.current = apply;
        if (flushFrameRef.current !== null) return;
        if (typeof requestAnimationFrame !== 'function') {
            flushDeltasNow();
            return;
        }
        flushFrameRef.current = requestAnimationFrame(() => {
            flushFrameRef.current = null;
            const pending = pendingFlushRef.current;
            pendingFlushRef.current = null;
            pending?.();
        });
    }

    /** Stop listening, leave the private channel, unbind the reconnect hook. */
    function leaveChannel() {
        const ref = echoRef.current;
        if (ref) {
            try { ref.channel.stopListening('.QueryStreamEvent'); } catch { /* noop */ }
            try { window.Echo?.leave?.(ref.name); } catch { /* noop */ }
            echoRef.current = null;
        }
        const listener = connectionListenerRef.current;
        if (listener) {
            try { window.Echo?.connector?.pusher?.connection?.unbind?.('state_change', listener); } catch { /* noop */ }
            connectionListenerRef.current = null;
        }
    }

    function endStream() {
        clearWatchdog();
        cancelDeltaFlush();
        leaveChannel();
        activeQueryRef.current = null;
        setStreaming(false);
    }

    /** The fatal-idle outcome when the server has no finished answer either. */
    function markStalled(assistantId: string) {
        // Two different situations wear the same timeout. "The answer
        // above may be incomplete" is only true when there IS an answer;
        // said over an empty bubble it sends the reader looking for text
        // that never arrived.
        const partial = 'The stream went quiet for 2 minutes and the server has no finished answer yet. The text above is unchecked and may be incomplete.';
        const nothing = 'The stream went quiet for 2 minutes and nothing arrived. Retry the question.';
        flushDeltasNow();
        setMessages((prev) => prev.map((m) => (m.id === assistantId && m.isStreaming
            ? {
                  ...m,
                  // Preserve whatever streamed. Nothing is synthesized
                  // into content — the error row below the bubble is
                  // the one place the message belongs.
                  status: null,
                  error: m.content ? partial : nothing,
                  isStreaming: false,
                  // The text preserved above is the RAW stream. Every
                  // §04i guard, the Layer-2 orphan-marker strip and the
                  // confidence floor run server-side after generation
                  // and only ever reach the browser on the completed
                  // frame, which never arrived here.
                  validationState: 'unverified',
              }
            : m)));
        endStream();
    }

    function armWatchdog(assistantId: string) {
        clearWatchdog();
        const warn = setTimeout(() => {
            setMessages((prev) => prev.map((m) => (m.id === assistantId && m.isStreaming
                ? { ...m, status: 'Still working… large queries can take a few minutes.' }
                : m)));
        }, 30_000);
        const fatal = setTimeout(() => {
            watchdogRef.current = null;
            const active = activeQueryRef.current;
            if (active && active.assistantId === assistantId) {
                void recoverFromServer(active).then((settled) => {
                    if (!settled) markStalled(assistantId);
                });
                return;
            }
            markStalled(assistantId);
        }, 120_000);
        watchdogRef.current = { assistantId, warn, fatal };
    }

    // Snapshot ref for event handlers that need current messages outside
    // a state updater (see applyCompleted).
    const messagesRef = useRef<ChatMessage[]>(messages);
    useEffect(() => { messagesRef.current = messages; }, [messages]);
    const streamingRef = useRef(streaming);
    useEffect(() => { streamingRef.current = streaming; }, [streaming]);

    // Sync to initialMessages on thread switch ONLY. Keying this on the
    // initialMessages array identity re-ran it on every Inertia prop
    // delivery (partial reloads, workspace hooks), wiping the in-flight
    // streaming bubble and resetting a freshly-minted conversation id
    // mid-answer. Thread switching is disabled while a stream is live
    // (CHAT-12), so the bail below is belt and braces.
    useEffect(() => {
        if (streamingRef.current) return;
        setMessages(initialMessages);
        setConversationId(active_thread_id ?? '');
        stickToBottomRef.current = true;
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [active_thread_id]);

    // Follow new tokens only while the reader is already at (or within a few
    // lines of) the bottom. Scrolling up to re-read an earlier answer while
    // a new one streams must not be fought.
    useEffect(() => {
        const el = scrollerRef.current;
        if (el && stickToBottomRef.current) el.scrollTop = el.scrollHeight;
    }, [messages]);

    function onTranscriptScroll() {
        const el = scrollerRef.current;
        if (!el) return;
        stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_TO_BOTTOM_PX;
    }

    function selectThread(id: string) {
        // CHAT-12 — switching mid-answer showed thread B's title over
        // thread A's transcript, and the next question went into A.
        if (streamingRef.current) return;
        router.get(`/projects/${project.slug}/chat`, { thread: id }, { preserveState: true });
        setMobileThreadsOpen(false);
    }

    function newThread() {
        // CHAT-3 — "+ New" mid-answer used to clear the transcript while
        // the listener stayed live; the late completed frame then persisted
        // the EMPTY transcript under the OLD thread id and the server's
        // full-replace erased the whole previous conversation.
        if (streamingRef.current) return;
        // Local reset only. Do NOT navigate — Foundry/ChatController auto-
        // selects the most recent thread when ?thread= is missing, so an
        // Inertia visit here would round-trip the page right back to the
        // previous conversation.
        setMessages([]);
        setConversationId('');
        setComposer('');
        setMobileThreadsOpen(false);
        if (typeof window !== 'undefined' && window.history && window.location.search) {
            window.history.replaceState(
                window.history.state,
                '',
                `/projects/${project.slug}/chat`,
            );
        }
    }

    function stopStreaming() {
        // CHAT-18 — leaving the channel alone left the Horizon job and the
        // FastAPI run going for up to 180 s, billed and holding an llm slot.
        const active = activeQueryRef.current;
        // queryId is '' until POST /queries answers; there is nothing to cancel yet.
        if (active?.queryId) {
            void fetch(`/api/v1/queries/${active.queryId}/cancel`, {
                method: 'POST',
                credentials: 'same-origin',
                headers: jsonHeaders(),
            }).catch(() => { /* best-effort: the job still ends on its own */ });
        }
        const convoId = active?.convoId ?? activeConvoIdRef.current;
        // Whatever was buffered is on screen too; keep all of it.
        flushDeltasNow();
        endStream();
        // Stopped mid-generation: the partial text on screen never reached
        // validate_node either. Same reasoning as the watchdog path above.
        // The error row says so in words (a reader who did not press Stop,
        // or who reloads, must not mistake the fragment for an answer) and
        // the turn is persisted so the fragment and that note survive.
        const next = messagesRef.current.map((m) =>
            m.isStreaming
                ? {
                      ...m,
                      isStreaming: false,
                      status: null,
                      validationState: 'unverified' as const,
                      error: m.content ? STOPPED_PARTIAL : STOPPED_EMPTY,
                  }
                : m,
        );
        messagesRef.current = next;
        setMessages(next);
        if (convoId) void persistConversation(convoId, next);
    }

    // CHAT-21 — on unmount (an Inertia navigation away mid-stream) leave the
    // private channel too, not just the timers; subscriptions used to pile
    // up per visit until a full reload.
    useEffect(() => () => {
        clearWatchdog();
        cancelDeltaFlush();
        leaveChannel();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    async function persistConversation(convoId: string, msgs: ChatMessage[]) {
        const title = (msgs.find((m) => m.role === 'user')?.content ?? 'New thread').slice(0, 80);
        try {
            const resp = await fetch(`/api/v1/conversations/${convoId}`, {
                method: 'PUT',
                credentials: 'same-origin',
                headers: jsonHeaders(),
                body: JSON.stringify({
                    title,
                    project_id: project.project_id,
                    messages: msgs.map((m) => toPersistedMessage(m)),
                }),
            });
            // CHAT-4 — a rejected sync used to be invisible; every later
            // turn then silently failed to save too.
            setPersistError(resp.ok ? null : `This thread could not be saved (HTTP ${resp.status}). Answers above will be lost on reload.`);
            // Refresh the rail (a new thread, a new title) without touching
            // `messages` or `active_thread_id`: the thread-sync effect above is
            // keyed on active_thread_id, so leaving it out of `only` keeps the
            // transcript on screen exactly as it is. (router.reload already
            // preserves component state; its options type has no preserveState.)
            if (resp.ok) router.reload({ only: ['threads', 'active_thread'] });
        } catch {
            setPersistError('This thread could not be saved (network error). Answers above will be lost on reload.');
        }
    }

    /**
     * Render a finished answer into the assistant bubble — from the live
     * completed frame, or from GET /queries/:id/result when the frame was
     * lost (CHAT-8). Returns false when the bubble no longer exists.
     */
    function applyCompleted(event: Record<string, unknown>, assistantId: string, convoId: string, streamedText: string, runningCitations: Citation[]): boolean {
        // CHAT-3 — the bubble is gone (thread reset): persisting now would
        // write this transcript under the wrong thread.
        if (!messagesRef.current.some((m) => m.id === assistantId)) {
            endStream();
            return false;
        }
        const finalText = String(event.text ?? streamedText);
        const finalConfidence = typeof event.confidence === 'number' ? event.confidence : null;
        // Backend: GeoRAGResponse.validation_state, set by validate_node.
        let finalValidationState: ValidationState = normaliseValidationState(event.validation_state);
        const finalCitations = (Array.isArray(event.citations) && event.citations.length > 0 ? event.citations : runningCitations) as Citation[];
        const answerRunId = event.answer_run_id ? String(event.answer_run_id) : null;
        // M2 P5 — viz_payload (chart hint) + map_payload (GeoJSON) ride on the
        // completed event. Backend: agentic_retrieval/nodes.py
        // (_build_chat_card_payloads).
        const finalMapPayload = (event.map_payload as Record<string, unknown> | null | undefined) ?? null;
        const finalVizPayload = (event.viz_payload as Record<string, unknown> | null | undefined) ?? null;
        // Plan §3a/§3b — typed evidence packet (GeoRAGResponse.evidence_packet).
        const finalEvidencePacket = (event.evidence_packet as Record<string, unknown> | null | undefined) ?? null;
        // Plan §3e — multi-turn resolution audit for the "Interpreted as:" chip.
        const finalMultiTurn = (event.multi_turn_resolution as Record<string, unknown> | null | undefined) ?? null;
        // §10u — structured refusal payload. Layer 1's hard refusal stamps
        // it unconditionally since CHAT-10.
        const finalRefusalPayload = (event.refusal_payload as ChatMessage['refusalPayload']) ?? null;
        // CHAT-16 — citations are mandatory (CLAUDE.md hard rule 4). An
        // answer that arrives with none is an upstream defect; keep the
        // answer visible but say plainly it is unverified.
        const citationsMissing = isUncitedAnswer(finalCitations, finalRefusalPayload);
        if (citationsMissing) finalValidationState = 'unverified';
        const truncatedFields = Array.isArray(event.truncated_fields) ? (event.truncated_fields as string[]) : null;

        // Built from the ref snapshot (not inside the state updater) so the
        // fire-and-forget persistence below is NOT a side effect of a React
        // updater — updaters are re-invoked under StrictMode/concurrent
        // renders, which duplicated the PUT per completed answer.
        const next = messagesRef.current.map((m) =>
            m.id === assistantId
                ? {
                      ...m,
                      content: finalText,
                      confidence: finalConfidence,
                      validationState: finalValidationState,
                      citations: finalCitations,
                      answer_run_id: answerRunId,
                      status: null,
                      error: null,
                      errorCode: null,
                      isStreaming: false,
                      mapPayload: finalMapPayload,
                      vizPayload: finalVizPayload,
                      evidencePacket: finalEvidencePacket,
                      multiTurnResolution: finalMultiTurn,
                      refusalPayload: finalRefusalPayload,
                      citationsMissing,
                      truncatedFields: event.payload_truncated ? truncatedFields : null,
                  }
                : m,
        );
        messagesRef.current = next;
        setMessages(next);
        void persistConversation(convoId, next);
        endStream();
        return true;
    }

    function applyFailed(event: Record<string, unknown>, assistantId: string) {
        flushDeltasNow();
        const errMsg = String(event.error ?? event.message ?? 'Query failed');
        // Typed code off classify_error() (app/agent/errors.py) or the job.
        const errCode = event.code !== undefined && event.code !== null ? String(event.code) : null;
        const next = messagesRef.current.map((m) =>
            m.id === assistantId
                ? {
                      ...m,
                      errorCode: errCode,
                      // Keep whatever streamed; the error row below the
                      // bubble is where the failure is said.
                      status: null,
                      error: errMsg,
                      isStreaming: false,
                      // Every §04i guard runs server-side and only reaches
                      // the browser on completed, which never arrived.
                      validationState: m.content ? ('unverified' as const) : m.validationState,
                  }
                : m,
        );
        messagesRef.current = next;
        setMessages(next);
        endStream();
        // Persist the failed turn too (CHAT-4): the server now accepts an
        // empty assistant message carrying its error.
        const convoId = activeConvoIdRef.current;
        if (convoId) void persistConversation(convoId, next);
    }

    /**
     * CHAT-8 — ask the audit row for the outcome of a query whose terminal
     * frame never arrived. Resolves true when the bubble was settled
     * (answer or failure), false when the server has nothing final yet.
     */
    async function recoverFromServer(active: ActiveQuery): Promise<boolean> {
        // No query id yet: POST /queries has not answered, so there is nothing to ask about.
        if (!active.queryId) return false;
        flushDeltasNow();
        try {
            const resp = await fetch(`/api/v1/queries/${active.queryId}/result`, {
                credentials: 'same-origin',
                headers: jsonHeaders(),
            });
            if (!resp.ok) return false;
            const body = (await resp.json()) as Record<string, unknown>;
            if (body.status === 'completed') {
                const current = messagesRef.current.find((m) => m.id === active.assistantId);
                return applyCompleted(body, active.assistantId, active.convoId, current?.content ?? '', current?.citations ?? []);
            }
            if (body.status === 'failed') {
                applyFailed(body, active.assistantId);
                return true;
            }
            return false;
        } catch {
            return false;
        }
    }

    /** Poll recovery a few times — the audit row is written just after the stream drains. */
    async function recoverWithRetry(active: ActiveQuery, attempts = 5): Promise<boolean> {
        for (let i = 0; i < attempts; i++) {
            if (await recoverFromServer(active)) return true;
            await new Promise((resolve) => setTimeout(resolve, 2_000));
        }
        return false;
    }

    async function sendMessage(text: string) {
        const query = text.trim();
        if (!query || streaming) return;
        if (!window.Echo) {
            // Surface in-conversation instead of window.alert().
            console.error('GeoRAG chat: window.Echo unavailable — Reverb may be down.');
            setMessages((prev) => [...prev, {
                id: newUuid(),
                role: 'assistant' as const,
                content: '',
                created_at: new Date().toISOString(),
                citations: [],
                confidence: null,
                answer_run_id: null,
                error: 'Live updates are unavailable right now, so this question was not sent. Reload the page and try again.',
            }]);
            return;
        }

        // First message in this session: mint a conversation id.
        const convoId = conversationId || newUuid();
        if (!conversationId) setConversationId(convoId);
        activeConvoIdRef.current = convoId;

        const userMsg: ChatMessage = {
            id: newUuid(),
            role: 'user',
            content: query,
            created_at: new Date().toISOString(),
            citations: [],
            confidence: null,
            answer_run_id: null,
        };
        const assistantId = newUuid();
        const assistantMsg: ChatMessage = {
            id: assistantId,
            role: 'assistant',
            content: '',
            created_at: new Date().toISOString(),
            citations: [],
            confidence: null,
            answer_run_id: null,
            status: 'Sending…',
            isStreaming: true,
            mapPayload: null,
            vizPayload: null,
            evidencePacket: null,
            multiTurnResolution: null,
            refusalPayload: null,
        };
        const withTurn = [...messagesRef.current, userMsg, assistantMsg];
        messagesRef.current = withTurn;
        setMessages(withTurn);
        setComposer('');
        setStreaming(true);
        // Sending is an explicit "show me the answer": follow it down.
        stickToBottomRef.current = true;

        // P0.2 watchdog — idle timers, re-armed on every received frame.
        armWatchdog(assistantId);

        // This send owns the stream only while it is the active query. Stop,
        // a newer send, or a terminal frame clears/replaces it; every deferred
        // callback below (the 5 s start fallback, the subscribe ACK, a late
        // failure) checks this first so it cannot start a cancelled run or
        // tear down someone else's stream. queryId is '' until POST /queries
        // answers.
        activeQueryRef.current = { queryId: '', assistantId, convoId };
        const isCurrent = () => activeQueryRef.current?.assistantId === assistantId;

        const failBeforeStream = (msg: string) => {
            if (!isCurrent()) return;
            endStream();
            setMessages((prev) =>
                prev.map((m) =>
                    m.id === assistantId ? { ...m, status: null, error: msg, isStreaming: false } : m,
                ),
            );
        };

        try {
            // Phase 1: open the query.
            const resp = await fetch('/api/v1/queries', {
                method: 'POST',
                credentials: 'same-origin',
                headers: jsonHeaders(),
                body: JSON.stringify({
                    query,
                    project_id: project.project_id,
                    // Phase 3 / Step 3.2 — context envelope shipped on /queries
                    // for validation only (the persisted side is /start).
                    // A raw_retrieval flag used to ride here from an "LLM
                    // synthesis: off" toggle that nothing server-side read
                    // (CHAT-11). Removed together with the toggle.
                    context_envelope: buildEnvelopePayload(envelope),
                }),
            });
            if (!resp.ok) {
                const detail = await resp.text();
                throw new Error(`Query rejected (${resp.status}): ${detail.slice(0, 200)}`);
            }
            const { query_id, channel } = await resp.json();
            // Stopped (or superseded) while the query was being opened: do not
            // subscribe to, or start, a run nobody is waiting for.
            const active = activeQueryRef.current;
            if (!active || active.assistantId !== assistantId) return;
            active.queryId = String(query_id);

            // Phase 2: subscribe to the broadcast channel.
            // QueryStreamEvent broadcasts on a PrivateChannel (see
            // app/Events/QueryStreamEvent.php and routes/channels.php).
            const echoChannel = window.Echo.private(channel);
            echoRef.current = { channel: echoChannel, name: channel };

            const deltas = createDeltaBuffer();
            const seenEventIds = new Set<string>();
            const runningCitations: Citation[] = [];

            echoChannel.listen('.QueryStreamEvent', (event: Record<string, unknown>) => {
                const eventType = String(event.event ?? '');

                // CHAT-15 — a re-delivered frame (reconnect, backplane) is
                // applied once.
                const eventId = typeof event.event_id === 'string' ? event.event_id : '';
                if (eventId) {
                    if (seenEventIds.has(eventId)) return;
                    seenEventIds.add(eventId);
                }

                // Every received frame proves the pipeline is alive —
                // push the idle watchdog out rather than racing a
                // wall-clock timer against a long synthesis.
                if (eventType !== 'completed' && eventType !== 'failed' && eventType !== 'error') {
                    armWatchdog(assistantId);
                }

                // CHAT-6 — a heartbeat only proves liveness; the phase line
                // (or the still-working hint) stays as it is.
                if (isHeartbeat(event)) return;

                // The job wraps non-JSON SSE deltas as text while JSON
                // deltas carry token — accept both.
                const deltaToken = event.token ?? event.text;
                if (eventType === 'status' && event.message) {
                    patchMessage(assistantId, (m) => ({ ...m, status: String(event.message) }));
                } else if (eventType === 'delta' && deltaToken) {
                    // CHAT-15 — ordered by token_seq, not arrival.
                    const assembled = deltas.add(String(deltaToken), event.token_seq ?? event.seq, null);
                    if (assembled === null) return;
                    // Buffered: at most one state update per animation frame.
                    scheduleDeltaFlush(() => {
                        const text = deltas.text();
                        patchMessage(assistantId, (m) => ({ ...m, content: text, status: null }));
                    });
                } else if (eventType === 'citation') {
                    runningCitations.push({
                        citation_id: String(event.citation_id ?? ''),
                        citation_type: String(event.citation_type ?? ''),
                        source_chunk_id: String(event.source_chunk_id ?? ''),
                        document_title: event.document_title ? String(event.document_title) : undefined,
                        relevance_score: typeof event.relevance_score === 'number' ? event.relevance_score : undefined,
                        // PGEO extensions — carried through so CitationPGEODetail
                        // can render during live streaming.
                        corpus: (event.corpus as Citation['corpus']) ?? null,
                        jurisdiction_code: event.jurisdiction_code ? String(event.jurisdiction_code) : null,
                        jurisdiction_name: event.jurisdiction_name ? String(event.jurisdiction_name) : null,
                        license_summary: event.license_summary ? String(event.license_summary) : null,
                        license_url: event.license_url ? String(event.license_url) : null,
                        source_url: event.source_url ? String(event.source_url) : null,
                        staleness_seconds: typeof event.staleness_seconds === 'number' ? event.staleness_seconds : null,
                    });
                    const snapshot = [...runningCitations];
                    patchMessage(assistantId, (m) => ({ ...m, citations: snapshot }));
                } else if (eventType === 'completed') {
                    applyCompleted(event, assistantId, convoId, deltas.text(), runningCitations);
                } else if (eventType === 'failed' || eventType === 'error') {
                    if (event.recoverable === true) {
                        flushDeltasNow();
                        // CHAT-5 — the answer exists but its frame could not
                        // be delivered; fetch it rather than failing.
                        clearWatchdog();
                        leaveChannel();
                        setMessages((prev) => prev.map((m) => (m.id === assistantId ? { ...m, status: 'Loading the finished answer…' } : m)));
                        void recoverWithRetry(active).then((settled) => {
                            if (!settled) applyFailed(event, assistantId);
                        });
                        return;
                    }
                    applyFailed(event, assistantId);
                }
            });

            // CHAT-7 — a denied /broadcasting/auth used to be silent: no
            // subscription, the 5 s fallback started the (billed) run anyway,
            // and the user watched "Sending…" for two minutes.
            let subscriptionFailed = false;
            const withError = echoChannel as unknown as { error?: (cb: (err: unknown) => void) => void };
            withError.error?.((err: unknown) => {
                subscriptionFailed = true;
                console.error('GeoRAG chat: realtime channel subscription failed', err);
                failBeforeStream('Could not subscribe to the realtime channel (authorisation failed). Reload the page and try again.');
            });

            // CHAT-8 — a pusher-js reconnect mid-answer can swallow the
            // terminal frame. When the socket comes back, ask the server.
            const pusherConnection = window.Echo?.connector?.pusher?.connection;
            if (pusherConnection?.bind) {
                const onStateChange: ConnectionStateListener = (states) => {
                    if (states.current === 'connected' && states.previous !== 'connected' && activeQueryRef.current?.assistantId === assistantId) {
                        void recoverFromServer(active);
                    }
                };
                pusherConnection.bind('state_change', onStateChange);
                connectionListenerRef.current = onStateChange;
            }

            // Phase 3: dispatch the job — but only once the private-channel
            // subscription is ACKed. Echo.private() returns synchronously
            // and merely queues pusher:subscribe; on a fresh page load the
            // websocket itself is often still connecting, so starting
            // immediately let the Horizon job broadcast the whole stream
            // before the browser was listening. A 5s fallback fires the
            // start anyway so a missed ACK can't dead-lock the send — but
            // only over a CONNECTED socket (CHAT-7): starting with no socket
            // bills a full LLM run that is broadcast to no one.
            const startQuery = async () => {
                try {
                    const startResp = await fetch(`/api/v1/queries/${query_id}/start`, {
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: jsonHeaders(),
                        body: JSON.stringify({
                            context_envelope: buildEnvelopePayload(envelope),
                            // Plan §3e — forward the chat thread so the FastAPI
                            // bridge can load prior turns for multi-turn
                            // resolution.
                            conversation_id: convoId,
                        }),
                    });
                    if (!startResp.ok && startResp.status !== 409) {
                        const detail = await startResp.text();
                        throw new Error(`Failed to start query (${startResp.status}): ${detail.slice(0, 200)}`);
                    }
                } catch (e) {
                    failBeforeStream(e instanceof Error ? e.message : 'Network error');
                }
            };
            let startFired = false;
            const fireStart = () => {
                if (startFired || subscriptionFailed || !isCurrent()) return;
                startFired = true;
                void startQuery();
            };
            const subscribable = echoChannel as unknown as { subscribed?: (cb: () => void) => void };
            if (typeof subscribable.subscribed === 'function') {
                subscribable.subscribed(fireStart);
                setTimeout(() => {
                    if (startFired || subscriptionFailed || !isCurrent()) return;
                    const state = window.Echo?.connector?.pusher?.connection?.state;
                    if (typeof state === 'string' && state !== 'connected') {
                        startFired = true; // never start this one
                        failBeforeStream(`Realtime channel unavailable (socket ${state}), so the query was not started. Check your connection and retry.`);
                        return;
                    }
                    fireStart();
                }, 5_000);
            } else {
                fireStart();
            }
        } catch (e) {
            failBeforeStream(e instanceof Error ? e.message : 'Network error');
        }
    }

    // Retry goes through a ref so the callback handed to every MessageBubble
    // keeps one identity for the life of the page; a new closure per render
    // would defeat React.memo on the whole transcript.
    const sendMessageRef = useRef(sendMessage);
    useEffect(() => { sendMessageRef.current = sendMessage; });
    const handleRetry = useCallback((text: string) => { void sendMessageRef.current(text); }, []);

    function handleSubmit(e: React.FormEvent) {
        e.preventDefault();
        sendMessage(composer);
    }

    function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
        // Enter sends (the universal chat convention — requiring Ctrl+Enter
        // made a plain Enter insert a silent newline, which presented as
        // "my message didn't send"). Shift+Enter inserts a newline;
        // Ctrl/Cmd+Enter still sends for muscle memory. IME composition
        // (e.g. CJK input) is respected via isComposing.
        if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            sendMessage(composer);
        }
    }

    return (
        <AppLayout>
            <Head title={active_thread?.title ?? 'Chat — GeoRAG'} />

            <div className="flex-1 grid lg:grid-cols-[280px_1fr] overflow-hidden" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                {/* Thread rail — below `lg:` this is collapsed by default and
                    toggled via the hamburger button in the conversation
                    header; renders inline above the conversation instead of
                    a fixed overlay (same convention as FoundryShell's mobile
                    nav drawer). */}
                <aside
                    className={[
                        mobileThreadsOpen ? 'block' : 'hidden',
                        'lg:block max-h-[50vh] lg:max-h-none border-r overflow-y-auto',
                    ].join(' ')}
                    style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}
                >
                    <div className="px-3 py-3 flex items-center justify-between border-b" style={{ borderColor: 'var(--line-1)' }}>
                        <span className="text-[10px] font-mono uppercase tracking-[0.12em]" style={{ color: 'var(--fg-3)' }}>Threads · {threads.length}</span>
                        <button
                            type="button"
                            onClick={newThread}
                            // CHAT-3 — never mid-answer: see newThread().
                            disabled={streaming}
                            title={streaming ? 'Wait for the answer (or press Stop) before starting a new thread' : undefined}
                            className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border disabled:opacity-40 disabled:cursor-not-allowed"
                            style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                        >
                            + New
                        </button>
                    </div>
                    {threads.length === 0 ? (
                        <div className="px-3 py-6 text-center text-xs" style={{ color: 'var(--fg-3)' }}>
                            No threads yet.
                        </div>
                    ) : (
                        threads.map((t) => (
                            <button
                                key={t.id}
                                type="button"
                                onClick={() => selectThread(t.id)}
                                // CHAT-12 — switching mid-answer split the
                                // header from the transcript.
                                disabled={streaming}
                                className="w-full text-left px-3 py-2.5 border-b transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                                style={{
                                    borderColor: 'var(--line-1)',
                                    background: t.id === active_thread_id ? 'var(--accent-bg)' : 'transparent',
                                    color: t.id === active_thread_id ? 'var(--fg-0)' : 'var(--fg-2)',
                                }}
                            >
                                <div className="text-xs font-medium truncate">{t.title}</div>
                                <div className="text-[10px] font-mono uppercase tracking-wider mt-0.5" style={{ color: 'var(--fg-3)' }}>
                                    {formatWhen(t.updated)}
                                </div>
                            </button>
                        ))
                    )}
                </aside>

                {/* Active conversation */}
                <section className="flex flex-col overflow-hidden">
                    <header className="px-6 py-3 border-b flex items-center gap-3 shrink-0" style={{ borderColor: 'var(--line-1)' }}>
                        <button
                            type="button"
                            onClick={() => setMobileThreadsOpen((v) => !v)}
                            className="lg:hidden shrink-0 p-1 -ml-1"
                            style={{ color: 'var(--fg-3)' }}
                            aria-label="Toggle threads"
                            aria-expanded={mobileThreadsOpen}
                        >
                            <svg width={16} height={16} viewBox="0 0 24 24" fill="none">
                                <path d="M3 6h18 M3 12h18 M3 18h18" stroke="currentColor" strokeWidth="2" />
                            </svg>
                        </button>
                        <BrandDiamond size={14} />
                        <div className="flex-1">
                            <div className="text-sm font-medium" style={{ color: 'var(--fg-0)' }}>
                                {active_thread?.title ?? (messages.length === 0 ? 'New thread' : 'Untitled thread')}
                            </div>
                            <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                                {messages.length} messages · project {project.project_name}
                            </div>
                        </div>
                    </header>

                    <div
                        ref={scrollerRef}
                        onScroll={onTranscriptScroll}
                        role="log"
                        aria-label="Conversation"
                        aria-live="polite"
                        aria-relevant="additions text"
                        // Deferring announcements while an answer streams keeps a
                        // screen reader from reading every token flush.
                        aria-busy={streaming}
                        className="flex-1 overflow-y-auto px-6 py-4 space-y-4"
                    >
                        {messages.length === 0 ? (
                            <div className="flex flex-col items-center gap-6 py-8">
                                <div className="text-center max-w-xl">
                                    <div className="text-[10px] font-mono uppercase tracking-[0.14em] mb-2" style={{ color: 'var(--accent)' }}>
                                        GeoRAG · Project context loaded
                                    </div>
                                    <div className="text-lg" style={{ color: 'var(--fg-0)' }}>
                                        Ask anything about <span style={{ color: 'var(--fg-0)', fontWeight: 600 }}>{project.project_name}</span>
                                    </div>
                                    <div className="text-xs mt-2" style={{ color: 'var(--fg-2)' }}>
                                        Drill holes, geological reports, ore grades, derived intervals, audit log entries.
                                        Don't worry about phrasing — the retriever rewrites your query, resolves anaphora, and classifies intent before searching.
                                    </div>
                                </div>
                                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 w-full max-w-2xl">
                                    {chips.map((chip) => (
                                        <button
                                            key={chip.label}
                                            type="button"
                                            onClick={() => sendMessage(chip.query)}
                                            className="text-left text-xs px-3 py-2 rounded border hover:opacity-90 transition-opacity"
                                            style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)', color: 'var(--fg-1)' }}
                                        >
                                            <span style={{ color: 'var(--accent)' }}>→</span> {chip.label}
                                        </button>
                                    ))}
                                </div>
                            </div>
                        ) : (
                            messages.map((m, idx) => {
                                // For an errored assistant bubble, Retry re-sends the
                                // paired preceding user message (the nearest user turn
                                // above it).
                                const pairedUser =
                                    m.role === 'assistant' && m.error
                                        ? messages
                                              .slice(0, idx)
                                              .filter((x) => x.role === 'user')
                                              .pop()
                                        : undefined;
                                return (
                                    <MessageBubble
                                        key={m.id}
                                        m={m}
                                        projectId={project.project_id}
                                        projectSlug={project.slug}
                                        retryText={pairedUser?.content}
                                        retryDisabled={streaming}
                                        onRetry={handleRetry}
                                    />
                                );
                            })
                        )}
                    </div>

                    {/* Composer */}
                    <footer className="border-t px-6 py-3 shrink-0" style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}>
                        <div className="flex items-center gap-2 mb-2">
                            {/* CHAT-11 — an "LLM synthesis: off (raw retrieval)"
                                toggle lived here. Nothing server-side ever read
                                it: every query was synthesised by the LLM while
                                the UI claimed otherwise. Removed rather than
                                inventing a no-synthesis mode. */}
                            {persistError && (
                                <span role="status" className="text-[10px] font-mono" style={{ color: 'var(--warn)' }}>
                                    {persistError}
                                </span>
                            )}
                            {streaming && (
                                <button
                                    type="button"
                                    onClick={stopStreaming}
                                    aria-label="Stop generating"
                                    className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border ml-auto"
                                    style={{ color: 'var(--warn)', borderColor: 'var(--warn)', background: 'rgba(217,119,6,0.1)' }}
                                >
                                    ■ Stop
                                </button>
                            )}
                        </div>
                        {/* Phase 3 / Steps 3.2 + 3.3 — context envelope + mode toggle.
                            Collapsed by default; expanding reveals the 12 fields. */}
                        <ContextEnvelopeForm
                            project={project}
                            value={envelope}
                            onChange={setEnvelope}
                            disabled={streaming}
                        />
                        <form onSubmit={handleSubmit} className="flex gap-2">
                            <textarea
                                value={composer}
                                onChange={(e) => setComposer(e.target.value)}
                                placeholder={`Ask about ${project.project_name}…`}
                                aria-label="Ask a question"
                                rows={2}
                                disabled={streaming}
                                className="flex-1 text-sm px-3 py-2 rounded border resize-none disabled:opacity-60"
                                style={{ background: 'var(--bg-2)', color: 'var(--fg-0)', borderColor: 'var(--line-2)' }}
                                onKeyDown={onKeyDown}
                            />
                            <button
                                type="submit"
                                disabled={streaming || !composer.trim()}
                                aria-busy={streaming}
                                aria-label={streaming ? 'Sending' : 'Send'}
                                className="text-xs font-mono uppercase tracking-wider px-4 py-2 rounded border self-stretch disabled:opacity-40"
                                style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                            >
                                {streaming ? '…' : 'Send →'}
                            </button>
                        </form>
                        <div className="text-[10px] font-mono uppercase tracking-wider mt-1.5" style={{ color: 'var(--fg-3)' }}>
                            enter sends · shift+enter newline
                        </div>
                    </footer>
                </section>
            </div>
        </AppLayout>
    );
}

/**
 * Pill tone for a retrieval-confidence score.
 *
 * The thresholds are the ones the backend already acts on, not new ones:
 * `_floor_confidence_with_warning_banner` floors a guard-failed answer to
 * 0.2, and `_compute_confidence` returns 0.1 for a refusal or a
 * no-tool-call answer. So <= 0.2 is "the backend deliberately pushed this
 * down" and < 0.5 is "barely anything scored".
 */
function confidenceTone(value: number): 'accent' | 'warn' | 'danger' {
    if (value <= 0.2) return 'danger';
    if (value < 0.5) return 'warn';
    return 'accent';
}

/**
 * Human label for a citation chip: its place in the answer and the document
 * it points at. Never the raw chunk id — that is an internal handle.
 */
function citationLabel(c: Citation, index: number): string {
    const doc = c.document_title || c.citation_type || 'source';
    return index >= 0 ? `[${index + 1}] ${doc}` : doc;
}

/**
 * Two chips are the same citation by id when they have one. Streamed
 * citations can arrive with an empty citation_id; comparing '' === '' marked
 * every such chip as the open one, so fall back to the chunk id.
 */
function isSameCitation(a: Citation, b: Citation): boolean {
    if (a === b) return true;
    if (a.citation_id !== '' || b.citation_id !== '') return a.citation_id === b.citation_id;
    return a.source_chunk_id !== '' && a.source_chunk_id === b.source_chunk_id;
}

const MessageBubble = memo(function MessageBubble({
    m,
    projectId,
    projectSlug,
    retryText,
    retryDisabled,
    onRetry,
}: {
    m: ChatMessage;
    projectId?: string | null;
    projectSlug: string;
    /** The user turn this bubble answered; Retry re-sends it. */
    retryText?: string;
    retryDisabled?: boolean;
    onRetry: (text: string) => void;
}) {
    const isUser = m.role === 'user';
    // Copy-to-clipboard with a brief confirmation flash.
    const [copied, setCopied] = useState(false);
    const copiedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    useEffect(() => () => {
        if (copiedTimerRef.current) clearTimeout(copiedTimerRef.current);
    }, []);

    function copyContent() {
        if (!m.content) return;
        navigator.clipboard
            ?.writeText(m.content)
            .then(() => {
                setCopied(true);
                if (copiedTimerRef.current) clearTimeout(copiedTimerRef.current);
                copiedTimerRef.current = setTimeout(() => setCopied(false), 1500);
            })
            .catch(() => {
                // Clipboard unavailable (permissions / insecure context) — no-op.
            });
    }
    // Citation chips were inert (title-attribute tooltip only) despite the
    // platform's citation-first pitch — GET /api/v1/citations/resolve
    // already existed server-side with nothing in the UI calling it.
    //
    // Built 2026-09-24 (§10s) — a non-PGEO chip now opens EvidenceInspector,
    // a slide-in Sheet, instead of expanding inline; EvidenceInspector owns
    // its own /citations/resolve fetch. PGEO citations keep their existing
    // inline <CitationPGEODetail> expand (a distinct, already-built citation
    // detail view with its own resolution flow — not what the inspector
    // should replace).
    const [inspectorCitation, setInspectorCitation] = useState<Citation | null>(null);
    const [expandedPgeo, setExpandedPgeo] = useState<string | null>(null);
    // §10p feedback hook from the inspector's "Report citation issue"
    // button — a fresh object identity on every click so FeedbackControls'
    // effect re-opens the down-vote form even on a repeat click for the
    // same category.
    const [feedbackPreset, setFeedbackPreset] = useState<{ category: 'citation_issue'; note?: string } | null>(null);

    function handleCitationClick(c: Citation) {
        if (c.citation_type === 'PGEO') {
            setExpandedPgeo((prev) => (prev === c.source_chunk_id ? null : (c.source_chunk_id ?? null)));
            return;
        }
        setInspectorCitation(c);
    }

    function handleReportCitationIssue(c: Citation) {
        setInspectorCitation(null);
        // Name the citation in the note so the report says which one it is
        // about; the geologist adds the "why".
        const n = m.citations.indexOf(c);
        setFeedbackPreset({ category: 'citation_issue', note: `Citation ${citationLabel(c, n)}: ` });
    }

    return (
        <div className={`group flex ${isUser ? 'justify-end' : 'justify-start'}`}>
            <div className="max-w-[80%]">
                <div
                    className="rounded-lg px-4 py-3 text-sm leading-relaxed whitespace-pre-wrap"
                    style={{
                        background: isUser ? 'var(--bg-2)' : 'var(--bg-1)',
                        border: '1px solid var(--line-1)',
                        color: 'var(--fg-1)',
                    }}
                >
                    {m.isStreaming && m.status && !m.content && (
                        <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--accent)' }}>
                            ● {m.status}
                        </span>
                    )}
                    {m.content}
                    {m.isStreaming && m.content && (
                        <span className="inline-block ml-1 animate-pulse" style={{ color: 'var(--accent)' }}>▍</span>
                    )}
                </div>
                {/* Built 2026-09-24 (§10u) — a typed refusal/failure panel
                    replaces the old plain-text error footnote. Prefers the
                    structured `refusal_payload` (completed frame, terminal
                    guard strategy) over the `failed` frame's error/code —
                    the two are mutually exclusive by construction (see the
                    stream handler above), so this is just precedence, not a
                    real conflict. */}
                {!isUser && m.refusalPayload && (
                    <RefusalPanel
                        variant="refusal"
                        message={m.refusalPayload.message ?? 'The answer was refused.'}
                        code={m.refusalPayload.reason_code ?? null}
                        guardCodes={m.refusalPayload.guard_codes ?? null}
                    />
                )}
                {!isUser && !m.refusalPayload && m.error && (
                    <RefusalPanel variant="failed" message={m.error} code={m.errorCode ?? null} />
                )}
                {/* CHAT-16 — CLAUDE.md hard rule 4: every RAG answer carries
                    citations. One that arrived with none is an upstream
                    defect; the answer stays visible but must not read like
                    a checked one. */}
                {!isUser && !m.isStreaming && m.citationsMissing && (
                    <div
                        role="alert"
                        data-testid="no-citations-warning"
                        className="mt-2 rounded-md border px-3 py-2 text-xs leading-relaxed"
                        style={{ borderColor: 'var(--warn, #d97706)', color: 'var(--warn, #d97706)' }}
                    >
                        No citations were returned for this answer — treat it as unverified.
                        Nothing above is backed by a source the system could point to.
                    </div>
                )}
                {/* CHAT-5 — the completed frame was too large for the
                    realtime channel and the job sent a slim one. The answer
                    and its citations are complete; the visualisation is not. */}
                {!isUser && m.truncatedFields && m.truncatedFields.length > 0 && (
                    <div data-testid="payload-truncated-note" className="mt-2 text-[11px]" style={{ color: 'var(--fg-3)' }}>
                        The chart/map for this answer was too large to deliver over the realtime channel and was left out
                        ({m.truncatedFields.join(', ')}). The answer text and citations are complete.
                    </div>
                )}
                {/* M2 P5 — inline visualizations (map / strip log / timeline / stereonet /
                    3D drill traces / coverage table) ride on completed event's
                    map_payload + viz_payload. InlineViz no-ops when both are null. */}
                {!isUser && (m.mapPayload || m.vizPayload) && (
                    <div className="mt-2">
                        <MemoInlineViz
                            mapPayload={m.mapPayload as Parameters<typeof InlineViz>[0]['mapPayload']}
                            vizPayload={m.vizPayload as Parameters<typeof InlineViz>[0]['vizPayload']}
                            projectId={projectId ?? null}
                        />
                    </div>
                )}
                {/* Plan §3a/§3b — typed evidence summary strip. Shows
                    per-kind counts (documents / tables / assays / collars /
                    spatial / graph) + a budget-pressure pill. Renders
                    nothing when the agentic graph wasn't engaged. */}
                {!isUser && <EvidencePacketBadge packet={m.evidencePacket} />}
                {/* Plan §3e — multi-turn resolution preview chip. Shows
                    "Interpreted as: …" when the resolve_node rewrote the
                    user's query. Renders nothing when the flag was off
                    or no rewrite happened. */}
                {!isUser && <ResolutionPreviewChip resolution={m.multiTurnResolution} />}
                {/* Built 2026-09-24 (§10p) — 👍/👎 + taxonomy + note on a
                    settled answer. No-ops while answer_run_id is null
                    (streaming, or errored before a run was persisted). */}
                {!isUser && (
                    <FeedbackControls answerRunId={m.answer_run_id} presetCategory={feedbackPreset} />
                )}
                <div className="flex items-center gap-2 mt-1.5 text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                    <span>{m.role}</span>
                    <span>·</span>
                    <span>{formatTime(m.created_at)}</span>
                    {/* `conf` is RETRIEVAL strength — see
                        GeoRAGResponse.confidence. It rendered tone="info" at
                        every value, so a floored 0.05 answer and a strong 0.95
                        one looked identical. The tone now tracks the number. */}
                    {m.confidence !== null && (
                        <>
                            <span>·</span>
                            <Pill tone={confidenceTone(m.confidence)}>
                                conf {m.confidence.toFixed(2)}
                            </Pill>
                        </>
                    )}
                    {/* The answer-level verdict, rendered independently of the
                        confidence number. It has to be independent: the two
                        states that most need saying — a stream that was cut
                        off, and one still in flight — have no confidence value
                        at all, because that only arrives on `completed`.
                        Nesting this inside the block above is what hid them. */}
                    {!isUser && (m.isStreaming || (m.validationState && m.validationState !== 'clean')) && (
                        <>
                            <span>·</span>
                            <Pill tone={m.validationState === 'flagged' ? 'danger' : 'warn'} dot>
                                {m.isStreaming
                                    ? 'not yet fact-checked'
                                    : m.validationState === 'flagged'
                                      ? 'fact-check flagged'
                                      : 'unverified'}
                            </Pill>
                        </>
                    )}
                    {/* Copy is a hover/focus affordance on any settled assistant
                        message — quiet in the transcript. Retry is NOT: on a
                        touch screen there is no hover, so an errored bubble
                        always shows it. */}
                    {!isUser && !m.isStreaming && m.content && (
                        <span className="inline-flex items-center gap-2 ml-1 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
                            <button
                                type="button"
                                onClick={copyContent}
                                aria-label="Copy message to clipboard"
                                className="font-mono text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded border"
                                style={{
                                    color: copied ? 'var(--accent)' : 'var(--fg-3)',
                                    borderColor: copied ? 'var(--accent)' : 'var(--line-2)',
                                    background: 'transparent',
                                }}
                            >
                                {copied ? '✓ copied' : 'copy'}
                            </button>
                        </span>
                    )}
                    {!isUser && !m.isStreaming && m.error && retryText !== undefined && (
                        <button
                            type="button"
                            onClick={() => onRetry(retryText)}
                            disabled={retryDisabled}
                            aria-label="Retry the question that produced this error"
                            className="ml-1 font-mono text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded border disabled:opacity-40"
                            style={{
                                color: 'var(--warn)',
                                borderColor: 'var(--warn)',
                                background: 'transparent',
                            }}
                        >
                            ↻ retry
                        </button>
                    )}
                </div>
                {m.citations.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-1.5">
                        {m.citations.map((c, i) => (
                            <button
                                key={c.citation_id || i}
                                type="button"
                                onClick={() => c.source_chunk_id && handleCitationClick(c)}
                                className="text-[10px] font-mono px-1.5 py-0.5 rounded border cursor-pointer"
                                style={{
                                    color: 'var(--fg-2)',
                                    borderColor:
                                        expandedPgeo === c.source_chunk_id || (inspectorCitation !== null && isSameCitation(inspectorCitation, c))
                                            ? 'var(--accent)'
                                            : 'var(--line-2)',
                                    background: 'transparent',
                                }}
                                title={citationLabel(c, i)}
                            >
                                [{i + 1}] {c.document_title ?? c.citation_type ?? '—'}
                                {typeof c.relevance_score === 'number' && (
                                    <span style={{ color: 'var(--fg-3)' }}> · {(c.relevance_score * 100).toFixed(0)}%</span>
                                )}
                            </button>
                        ))}
                    </div>
                )}
                {/* PGEO citations keep their existing inline expand — see the
                    handleCitationClick docblock above for why. Non-PGEO
                    citations open <EvidenceInspector> instead, mounted
                    below. */}
                {m.citations.map((c, i) => {
                    if (c.citation_type !== 'PGEO' || !c.source_chunk_id || expandedPgeo !== c.source_chunk_id) {
                        return null;
                    }
                    const pgeoCitation: SharedCitation = {
                        citation_id: c.citation_id,
                        citation_type: 'PGEO',
                        source_chunk_id: c.source_chunk_id,
                        document_title: c.document_title ?? '',
                        relevance_score: c.relevance_score ?? 0,
                        corpus: c.corpus,
                        jurisdiction_code: c.jurisdiction_code,
                        jurisdiction_name: c.jurisdiction_name,
                        license_summary: c.license_summary,
                        license_url: c.license_url,
                        source_url: c.source_url,
                        staleness_seconds: c.staleness_seconds,
                    };
                    return (
                        <div
                            key={`resolved-${c.citation_id || i}`}
                            className="mt-1.5 rounded-lg px-3 py-3"
                            style={{ background: 'var(--bg-2)', border: '1px solid var(--line-1)' }}
                        >
                            <CitationPGEODetail citation={pgeoCitation} />
                        </div>
                    );
                })}
            </div>
            {/* Built 2026-09-24 (§10s) — Evidence Inspector Sheet, opened by
                a non-PGEO citation chip click above. */}
            <EvidenceInspector
                citation={inspectorCitation}
                open={inspectorCitation !== null}
                onOpenChange={(open) => {
                    if (!open) setInspectorCitation(null);
                }}
                projectSlug={projectSlug}
                answerRunId={m.answer_run_id}
                onReportIssue={handleReportCitationIssue}
            />
        </div>
    );
});
