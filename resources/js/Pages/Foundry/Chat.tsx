import { useEffect, useRef, useState, useMemo } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { Pill, EmptyState, BrandDiamond } from '@/Components/Foundry/primitives';
import EvidencePacketBadge from '@/Components/EvidencePacketBadge';
import InlineViz from '@/Components/InlineViz';
import ResolutionPreviewChip from '@/Components/ResolutionPreviewChip';
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
 *   4. Stream events: status / routing / delta / citation / completed / failed
 *
 * On `completed`, persists the full conversation via PUT
 * /api/v1/conversations/{uuid} so the threads rail picks it up on next
 * page load.
 *
 * Outstanding feature work (not yet ported into this Foundry-shell page):
 *   - EvidenceInspector + TrustInspector side panels
 *   - Conflict-detection + freshness rendering
 *   - Map / viz payload rendering (M2 P5 visualization)
 *   - Follow-up suggestion rendering
 *   - WS-01 reconnect / event-dedup layer
 * These all live in the backend already (their payload fields land on
 * the completed event), so they can be layered in without server work.
 */

interface ChatThread { id: string; title: string; updated: string }
interface Citation {
    citation_id: string;
    citation_type: string;
    source_chunk_id: string;
    document_title?: string;
    relevance_score?: number;
}
interface ChatMessage {
    id: string;
    role: 'user' | 'assistant' | string;
    content: string;
    created_at: string;
    citations: Citation[];
    confidence: number | null;
    answer_run_id: string | null;
    status?: string | null;
    error?: string | null;
    isStreaming?: boolean;
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

// Six suggestion chips covering the main intent classes;
// copy worded for Wyoming roll-front uranium.
const SUGGESTION_CHIPS: Array<{ label: string; query: string }> = [
    { label: 'How many drill holes are in this project?', query: 'How many drill holes are in this project?' },
    { label: 'Summarise the deepest five holes', query: 'Summarise the deepest five drill holes with their total depth and ore intercepts.' },
    { label: 'What deposit does this project host?', query: 'What deposit style does this project host and what is its geological setting?' },
    { label: 'Top derived ore intervals', query: 'What are the top five derived ore intervals across all holes in this project?' },
    { label: 'Compare mean grade across holes', query: 'Compare the mean uranium grade across every hole in this project.' },
    { label: 'Recommend where to drill next', query: 'Where should the next drill hole be located based on existing grades and collar coverage?' },
];

const SLASH_COMMANDS = ['/compare', '/analog', '/permit', '/pin', '/map', '/branch'];

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

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const window: any;

export default function FoundryChat({ project, threads, active_thread_id, active_thread, messages: initialMessages }: ChatPageProps) {
    const [rawRetrieval, setRawRetrieval] = useState(false);
    const [composer, setComposer] = useState('');
    const [messages, setMessages] = useState<ChatMessage[]>(initialMessages);
    const [streaming, setStreaming] = useState(false);
    const [conversationId, setConversationId] = useState<string>(active_thread_id ?? '');

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
    const scrollerRef = useRef<HTMLDivElement | null>(null);
    // P0.2 — timeout watchdog. Two stage timers protect against the chat
    // ever sitting on "Sending…" forever when a terminal event is lost
    // (Reverb down, payload too large, network blip mid-stream).
    //   warn  @  30s — flip the status line to a "still working" notice
    //   fatal @  60s — replace the assistant message with a terminal
    //                  error + Retry affordance, tear down the channel
    const warnTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const fatalTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    function clearWatchdog() {
        if (warnTimerRef.current) { clearTimeout(warnTimerRef.current); warnTimerRef.current = null; }
        if (fatalTimerRef.current) { clearTimeout(fatalTimerRef.current); fatalTimerRef.current = null; }
    }

    // Sync to initialMessages on thread switch.
    useEffect(() => {
        setMessages(initialMessages);
        setConversationId(active_thread_id ?? '');
    }, [active_thread_id, initialMessages]);

    // Auto-scroll on new tokens.
    useEffect(() => {
        const el = scrollerRef.current;
        if (el) el.scrollTop = el.scrollHeight;
    }, [messages]);

    function selectThread(id: string) {
        router.get(`/projects/${project.slug}/chat`, { thread: id }, { preserveState: true });
    }

    function newThread() {
        // Local reset only. Do NOT navigate — Foundry/ChatController auto-
        // selects the most recent thread when `?thread=` is missing, so an
        // Inertia visit here would round-trip the page right back to the
        // previous conversation (the exact bug we're fixing).
        // sendMessage() mints a fresh conversation_id via
        // `conversationId || newUuid()` on the next user input, and
        // persistConversation() then writes the new thread to the server
        // so it appears in the sidebar on the next navigation.
        setMessages([]);
        setConversationId('');
        setComposer('');
        // Also clear any `?thread=` already in the URL so the browser
        // address bar matches the empty state.
        if (typeof window !== 'undefined' && window.history && window.location.search) {
            window.history.replaceState(
                window.history.state,
                '',
                `/projects/${project.slug}/chat`,
            );
        }
    }

    function stopStreaming() {
        clearWatchdog();
        const ref = echoRef.current;
        if (ref) {
            try { ref.channel.stopListening('.QueryStreamEvent'); } catch { /* noop */ }
            try { window.Echo?.leave?.(ref.name); } catch { /* noop */ }
            echoRef.current = null;
        }
        setStreaming(false);
        setMessages((prev) => prev.map((m) => (m.isStreaming ? { ...m, isStreaming: false, status: null } : m)));
    }

    // Clear timers on unmount so a navigation away mid-stream doesn't
    // leave a fatal-timer ticking against a stale assistant id.
    useEffect(() => () => clearWatchdog(), []);

    async function persistConversation(convoId: string, msgs: ChatMessage[]) {
        try {
            const title = (msgs.find((m) => m.role === 'user')?.content ?? 'New thread').slice(0, 80);
            await fetch(`/api/v1/conversations/${convoId}`, {
                method: 'PUT',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    Accept: 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                    ...(getCsrf() ? { 'X-CSRF-TOKEN': getCsrf() as string } : {}),
                },
                body: JSON.stringify({
                    title,
                    project_id: project.project_id,
                    messages: msgs.map((m) => ({
                        role: m.role,
                        content: m.content,
                        metadata: {
                            citations: m.citations,
                            confidence: m.confidence,
                            answer_run_id: m.answer_run_id,
                        },
                    })),
                }),
            });
        } catch {
            // Soft-fail: chat works without persistence.
        }
    }

    async function sendMessage(text: string) {
        const query = text.trim();
        if (!query || streaming) return;
        if (!window.Echo) {
            // eslint-disable-next-line no-alert
            alert('Realtime channel (Echo) not available. Reverb might be down.');
            return;
        }

        // First message in this session: mint a conversation id.
        const convoId = conversationId || newUuid();
        if (!conversationId) setConversationId(convoId);

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
        };
        setMessages((prev) => [...prev, userMsg, assistantMsg]);
        setComposer('');
        setStreaming(true);

        // P0.2 watchdog — kick off both timers. Any terminal event below
        // (completed / failed / error / Stop) calls clearWatchdog() so
        // they never fire on a successful run.
        clearWatchdog();
        warnTimerRef.current = setTimeout(() => {
            setMessages((prev) => prev.map((m) => (m.id === assistantId && m.isStreaming
                ? { ...m, status: 'Still working… large queries can take up to a minute.' }
                : m)));
        }, 30_000);
        fatalTimerRef.current = setTimeout(() => {
            const errMsg = 'No response within 60 seconds. The realtime channel may have dropped — try again.';
            setMessages((prev) => prev.map((m) => (m.id === assistantId && m.isStreaming
                ? { ...m, content: `Error: ${errMsg}`, status: null, error: errMsg, isStreaming: false }
                : m)));
            const ref = echoRef.current;
            if (ref) {
                try { ref.channel.stopListening('.QueryStreamEvent'); } catch { /* noop */ }
                try { window.Echo?.leave?.(ref.name); } catch { /* noop */ }
                echoRef.current = null;
            }
            setStreaming(false);
        }, 60_000);

        try {
            // Phase 1: open the query.
            const resp = await fetch('/api/v1/queries', {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    Accept: 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                    ...(getCsrf() ? { 'X-CSRF-TOKEN': getCsrf() as string } : {}),
                },
                body: JSON.stringify({
                    query,
                    project_id: project.project_id,
                    raw_retrieval: rawRetrieval,
                    // Phase 3 / Step 3.2 — context envelope shipped on /queries
                    // for validation only (the persisted side is /queries/{id}/start).
                    context_envelope: buildEnvelopePayload(envelope),
                }),
            });
            if (!resp.ok) {
                const detail = await resp.text();
                throw new Error(`Query rejected (${resp.status}): ${detail.slice(0, 200)}`);
            }
            const { query_id, channel } = await resp.json();

            // Phase 2: subscribe to the broadcast channel.
            // QueryStreamEvent broadcasts on a PrivateChannel (see
            // app/Events/QueryStreamEvent.php and routes/channels.php).
            // Echo.channel() is for PUBLIC channels and silently never
            // receives private-channel events — must use Echo.private().
            const echoChannel = window.Echo.private(channel);
            echoRef.current = { channel: echoChannel, name: channel };

            let accumulatedText = '';
            let runningCitations: Citation[] = [];

            echoChannel.listen('.QueryStreamEvent', (event: Record<string, unknown>) => {
                const eventType = String(event.event ?? '');

                if (eventType === 'status' && event.message) {
                    setMessages((prev) => prev.map((m) => (m.id === assistantId ? { ...m, status: String(event.message) } : m)));
                } else if (eventType === 'delta' && event.token) {
                    accumulatedText += String(event.token);
                    setMessages((prev) => prev.map((m) => (m.id === assistantId ? { ...m, content: accumulatedText, status: null } : m)));
                } else if (eventType === 'citation') {
                    runningCitations.push({
                        citation_id: String(event.citation_id ?? ''),
                        citation_type: String(event.citation_type ?? ''),
                        source_chunk_id: String(event.source_chunk_id ?? ''),
                        document_title: event.document_title ? String(event.document_title) : undefined,
                        relevance_score: typeof event.relevance_score === 'number' ? event.relevance_score : undefined,
                    });
                    setMessages((prev) => prev.map((m) => (m.id === assistantId ? { ...m, citations: [...runningCitations] } : m)));
                } else if (eventType === 'completed') {
                    const finalText = String(event.text ?? accumulatedText);
                    const finalConfidence = typeof event.confidence === 'number' ? event.confidence : null;
                    const finalCitations = Array.isArray(event.citations) && event.citations.length > 0 ? event.citations : runningCitations;
                    const answerRunId = event.answer_run_id ? String(event.answer_run_id) : null;
                    // M2 P5 — viz_payload (chart hint) + map_payload (GeoJSON) ride on the
                    // completed event. Backend: src/fastapi/app/agent/agentic_retrieval/nodes.py
                    // (_build_chat_card_payloads). Captured here so MessageBubble can render
                    // <InlineViz> below the answer bubble. Was missing in Foundry/Chat.tsx
                    // pre-2026-05-26 (see docblock "Not yet ported" list — now ported).
                    const finalMapPayload = (event.map_payload as Record<string, unknown> | null | undefined) ?? null;
                    const finalVizPayload = (event.viz_payload as Record<string, unknown> | null | undefined) ?? null;
                    // Plan §3a/§3b — capture the typed evidence packet
                    // off the completed event. Shape comes from
                    // GeoRAGResponse.evidence_packet (model_dump form).
                    // EvidencePacketBadge no-ops when null / empty.
                    const finalEvidencePacket =
                        (event.evidence_packet as Record<string, unknown> | null | undefined) ?? null;
                    // Plan §3e — multi-turn resolution audit for the
                    // "Interpreted as:" preview chip.
                    const finalMultiTurn =
                        (event.multi_turn_resolution as Record<string, unknown> | null | undefined) ?? null;
                    setMessages((prev) => {
                        const next = prev.map((m) =>
                            m.id === assistantId
                                ? {
                                      ...m,
                                      content: finalText,
                                      confidence: finalConfidence,
                                      citations: finalCitations as Citation[],
                                      answer_run_id: answerRunId,
                                      status: null,
                                      isStreaming: false,
                                      mapPayload: finalMapPayload,
                                      vizPayload: finalVizPayload,
                                      evidencePacket: finalEvidencePacket,
                                      multiTurnResolution: finalMultiTurn,
                                  }
                                : m,
                        );
                        // Fire-and-forget persistence.
                        persistConversation(convoId, next);
                        return next;
                    });
                    clearWatchdog();
                    try { echoChannel.stopListening('.QueryStreamEvent'); } catch { /* noop */ }
                    try { window.Echo.leave(channel); } catch { /* noop */ }
                    echoRef.current = null;
                    setStreaming(false);
                } else if (eventType === 'failed' || eventType === 'error') {
                    const errMsg = String(event.error ?? event.message ?? 'Query failed');
                    setMessages((prev) =>
                        prev.map((m) =>
                            m.id === assistantId
                                ? { ...m, content: `Error: ${errMsg}`, status: null, error: errMsg, isStreaming: false }
                                : m,
                        ),
                    );
                    clearWatchdog();
                    try { echoChannel.stopListening('.QueryStreamEvent'); } catch { /* noop */ }
                    try { window.Echo.leave(channel); } catch { /* noop */ }
                    echoRef.current = null;
                    setStreaming(false);
                }
            });

            // Phase 3: dispatch the job now that the listener is bound.
            // Phase 3 / Step 3.2 — forward the envelope here too; this is
            // the call that actually fires the Horizon job, so the envelope
            // travels with the job through to FastAPI.
            const startResp = await fetch(`/api/v1/queries/${query_id}/start`, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    Accept: 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                    ...(getCsrf() ? { 'X-CSRF-TOKEN': getCsrf() as string } : {}),
                },
                body: JSON.stringify({
                    context_envelope: buildEnvelopePayload(envelope),
                    // Plan §3e — forward the chat thread so the FastAPI
                    // bridge can load prior turns for multi-turn
                    // resolution. No-op when MULTI_TURN_RESOLUTION_ENABLED
                    // is False or the conversation has no prior turns.
                    conversation_id: convoId,
                }),
            });
            if (!startResp.ok && startResp.status !== 409) {
                const detail = await startResp.text();
                throw new Error(`Failed to start query (${startResp.status}): ${detail.slice(0, 200)}`);
            }
        } catch (e) {
            clearWatchdog();
            const msg = e instanceof Error ? e.message : 'Network error';
            setMessages((prev) =>
                prev.map((m) =>
                    m.id === assistantId ? { ...m, content: `Error: ${msg}`, status: null, error: msg, isStreaming: false } : m,
                ),
            );
            setStreaming(false);
        }
    }

    function handleSubmit(e: React.FormEvent) {
        e.preventDefault();
        sendMessage(composer);
    }

    function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            sendMessage(composer);
        }
    }

    return (
        <AppLayout>
            <Head title={active_thread?.title ?? 'Chat — GeoRAG'} />

            <div className="flex-1 grid grid-cols-[280px_1fr_280px] overflow-hidden" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                {/* Thread rail */}
                <aside className="border-r overflow-y-auto" style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}>
                    <div className="px-3 py-3 flex items-center justify-between border-b" style={{ borderColor: 'var(--line-1)' }}>
                        <span className="text-[10px] font-mono uppercase tracking-[0.12em]" style={{ color: 'var(--fg-3)' }}>Threads · {threads.length}</span>
                        <button
                            type="button"
                            onClick={newThread}
                            className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border"
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
                                className="w-full text-left px-3 py-2.5 border-b transition-colors"
                                style={{
                                    borderColor: 'var(--line-1)',
                                    background: t.id === active_thread_id ? 'var(--accent-bg)' : 'transparent',
                                    color: t.id === active_thread_id ? 'var(--fg-0)' : 'var(--fg-2)',
                                }}
                            >
                                <div className="text-xs font-medium truncate">{t.title}</div>
                                <div className="text-[10px] font-mono uppercase tracking-wider mt-0.5" style={{ color: 'var(--fg-3)' }}>
                                    {t.updated.slice(0, 16)}
                                </div>
                            </button>
                        ))
                    )}
                </aside>

                {/* Active conversation */}
                <section className="flex flex-col overflow-hidden">
                    <header className="px-6 py-3 border-b flex items-center gap-3 shrink-0" style={{ borderColor: 'var(--line-1)' }}>
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

                    <div ref={scrollerRef} className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
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
                                    {SUGGESTION_CHIPS.map((chip) => (
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
                            messages.map((m) => <MessageBubble key={m.id} m={m} projectId={project.project_id} />)
                        )}
                    </div>

                    {/* Composer */}
                    <footer className="border-t px-6 py-3 shrink-0" style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}>
                        <div className="flex items-center gap-2 mb-2">
                            <label className="flex items-center gap-2 text-[10px] font-mono uppercase tracking-wider cursor-pointer" style={{ color: 'var(--fg-2)' }}>
                                <input type="checkbox" checked={rawRetrieval} onChange={(e) => setRawRetrieval(e.target.checked)} />
                                LLM synthesis: {rawRetrieval ? <span style={{ color: 'var(--warn)' }}>off (raw retrieval)</span> : <span style={{ color: 'var(--accent)' }}>on</span>}
                            </label>
                            {streaming && (
                                <button
                                    type="button"
                                    onClick={stopStreaming}
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
                                rows={2}
                                disabled={streaming}
                                className="flex-1 text-sm px-3 py-2 rounded border resize-none disabled:opacity-60"
                                style={{ background: 'var(--bg-2)', color: 'var(--fg-0)', borderColor: 'var(--line-2)' }}
                                onKeyDown={onKeyDown}
                            />
                            <button
                                type="submit"
                                disabled={streaming || !composer.trim()}
                                className="text-xs font-mono uppercase tracking-wider px-4 py-2 rounded border self-stretch disabled:opacity-40"
                                style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                            >
                                {streaming ? '…' : 'Send →'}
                            </button>
                        </form>
                        <div className="text-[10px] font-mono uppercase tracking-wider mt-1.5" style={{ color: 'var(--fg-3)' }}>
                            ⌘/ctrl+enter sends · citations resolve via /api/v1/citations · stream via Reverb
                        </div>
                    </footer>
                </section>

                {/* Slash commands rail */}
                <aside className="border-l overflow-y-auto" style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}>
                    <div className="px-3 py-3 border-b text-[10px] font-mono uppercase tracking-[0.12em]" style={{ borderColor: 'var(--line-1)', color: 'var(--fg-3)' }}>
                        Slash commands
                    </div>
                    <ul className="px-3 py-2 space-y-1 text-[11px]">
                        {SLASH_COMMANDS.map((c) => (
                            <li key={c} className="font-mono" style={{ color: 'var(--fg-2)' }}>{c}</li>
                        ))}
                    </ul>
                    <div className="px-3 py-3 border-t text-[10px] font-mono uppercase tracking-wider" style={{ borderColor: 'var(--line-1)', color: 'var(--fg-3)' }}>
                        Branch tree
                    </div>
                    <div className="px-3 py-2 text-[11px]" style={{ color: 'var(--fg-3)' }}>
                        Use <code className="font-mono" style={{ color: 'var(--fg-2)' }}>/branch</code> to fork the current
                        thread; the graph renders when this conversation has ≥1 fork.
                    </div>
                </aside>
            </div>
        </AppLayout>
    );
}

function MessageBubble({ m, projectId }: { m: ChatMessage; projectId?: string | null }) {
    const isUser = m.role === 'user';
    return (
        <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
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
                    {m.error && (
                        <div className="mt-2 text-[10px] font-mono" style={{ color: 'var(--warn, #d97706)' }}>
                            {m.error}
                        </div>
                    )}
                </div>
                {/* M2 P5 — inline visualizations (map / strip log / timeline / stereonet /
                    3D drill traces / coverage table) ride on completed event's
                    map_payload + viz_payload. InlineViz no-ops when both are null. */}
                {!isUser && (m.mapPayload || m.vizPayload) && (
                    <div className="mt-2">
                        <InlineViz
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
                <div className="flex items-center gap-2 mt-1.5 text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                    <span>{m.role}</span>
                    <span>·</span>
                    <span>{m.created_at.slice(11, 16)}</span>
                    {m.confidence !== null && (
                        <>
                            <span>·</span>
                            <Pill tone="info">conf {m.confidence.toFixed(2)}</Pill>
                        </>
                    )}
                    {m.answer_run_id && (
                        <Link href={`/retrieval/${m.answer_run_id}`} className="ml-auto" style={{ color: 'var(--accent)' }}>
                            inspect retrieval →
                        </Link>
                    )}
                </div>
                {m.citations.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-1.5">
                        {m.citations.map((c, i) => (
                            <span
                                key={c.citation_id || i}
                                className="text-[10px] font-mono px-1.5 py-0.5 rounded border"
                                style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                                title={c.source_chunk_id}
                            >
                                [{i + 1}] {c.document_title ?? c.citation_type ?? '—'}
                                {typeof c.relevance_score === 'number' && (
                                    <span style={{ color: 'var(--fg-3)' }}> · {(c.relevance_score * 100).toFixed(0)}%</span>
                                )}
                            </span>
                        ))}
                    </div>
                )}
            </div>
        </div>
    );
}
