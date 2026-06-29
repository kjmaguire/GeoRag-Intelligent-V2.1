import { useState, useRef, useEffect, useCallback, type JSX } from 'react';
import { Head } from '@inertiajs/react';
import AppLayout from '../Layouts/AppLayout';
import type { SourceData, ChatMessage as ChatMessageType, ChatThread, Citation, RefusalPayload } from '@/types';
import ChatMessage from '../Components/ChatMessage';
import CitationPGEODetail from '../Components/PublicGeoscience/CitationPGEODetail';
import ProjectContextBanner from '../Components/ProjectContextBanner';
import { EvidenceInspector } from '../Components/chat/EvidenceInspector';
import { TrustInspector } from '../Components/chat/TrustInspector';
import { useEventDedup } from '../Hooks/useEventDedup';

// Initial welcome message shown before the user sends anything
// Extend window with Laravel Echo instance (set in bootstrap.js)
declare global {
    interface Window {
        // Audit 2026-06-28: typed `any` to match the other global Window.Echo
        // declaration (laravel-echo bootstrap) — conflicting shapes otherwise.
        Echo: any;
    }
}

interface EchoChannel {
    listen: (event: string, callback: (e: Record<string, unknown>) => void) => EchoChannel;
    stopListening: (event: string) => void;
}

const WELCOME_MESSAGE: ChatMessageType = {
    id: 'welcome',
    role: 'assistant',
    content:
        'Welcome to GeoRAG Intelligence. Ask me anything about your drill hole data, ' +
        'geological reports, or exploration datasets — or tap a suggestion below to get started.',
    timestamp: new Date().toISOString(),
};

/**
 * C3 — empty-state prompt chips. Six clickable examples covering the
 * classifier's main intent buckets (count/summary/document/structural/
 * assay/target) so first-time users see what the system is capable of
 * without reading documentation.
 */
const EMPTY_STATE_PROMPTS: { label: string; query: string; intent: string }[] = [
    {
        label: 'How many drill holes are in this project?',
        query: 'How many drill holes are in this project?',
        intent: 'count',
    },
    {
        label: 'Summarise the deepest five holes',
        query: 'Summarise the deepest five drill holes in this project with their total depth and hole type.',
        intent: 'summary',
    },
    {
        label: 'What deposit does this project host?',
        query: 'What deposit does this project host and what is its geological setting?',
        intent: 'narrative',
    },
    {
        label: 'Show the highest-grade gold intercepts',
        query: 'What are the top five highest-grade gold intercepts across all holes in this project?',
        intent: 'assay',
    },
    {
        label: 'Compare mean grade across holes',
        query: 'Compare the mean uranium grade across every hole in this project.',
        intent: 'assay',
    },
    {
        label: 'Recommend where to drill next',
        query: 'Where should the next drill hole be located based on existing grades and collar coverage?',
        intent: 'targeting',
    },
];

let messageIdCounter = 1;
function nextId() {
    return 'msg-' + messageIdCounter++;
}

/**
 * SourceViewer — fetches and displays the original source text for a citation.
 * Calls GET /api/v1/citations/resolve?source_chunk_id=...
 */
interface SourceViewerProps {
    sourceChunkId: string;
    citationType?: string;
}

function SourceViewer({ sourceChunkId, citationType }: SourceViewerProps): JSX.Element {
    const [sourceData, setSourceData] = useState<SourceData | null>(null);
    const [sourceLoading, setSourceLoading] = useState<boolean>(false);
    const [sourceError, setSourceError] = useState<string | null>(null);
    const [expanded, setExpanded] = useState<boolean>(false);

    async function loadSource(): Promise<void> {
        if (sourceData) {
            setExpanded(!expanded);
            return;
        }
        setSourceLoading(true);
        setSourceError(null);
        try {
            // Auth via Sanctum session cookie (same-origin). No bearer token from
            // localStorage — localStorage is an XSS-exfiltration target (types.ts:11-12).
            const res = await fetch(
                `/api/v1/citations/resolve?source_chunk_id=${encodeURIComponent(sourceChunkId)}&citation_type=${citationType || 'DATA'}`,
                {
                    credentials: 'same-origin',
                    headers: {
                        Accept: 'application/json',
                    },
                },
            );
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            setSourceData(data);
            setExpanded(true);
        } catch (err) {
            setSourceError(err instanceof Error ? err.message : String(err));
        } finally {
            setSourceLoading(false);
        }
    }

    return (
        <div className="mt-2">
            <button
                type="button"
                onClick={loadSource}
                className="text-xs text-amber-400 hover:text-amber-300 border border-amber-800/50 hover:border-amber-600/50 bg-amber-950/30 hover:bg-amber-950/50 rounded px-2 py-1 transition-colors w-full text-left"
            >
                {sourceLoading ? 'Loading source…' : expanded ? '▼ Hide original source' : '▶ View original source'}
            </button>

            {sourceError && (
                <p className="text-[10px] text-red-400 mt-1">Failed to load: {sourceError}</p>
            )}

            {expanded && sourceData && (
                <div className="mt-2 bg-gray-800 border border-gray-700 rounded-lg p-3 space-y-2">
                    <div className="flex items-center gap-2">
                        <span className="text-[10px] uppercase tracking-wider font-semibold text-amber-400 bg-amber-950/60 border border-amber-800/60 px-1.5 py-0.5 rounded">
                            {sourceData.source_type}
                        </span>
                        <span className="text-xs text-gray-300 font-medium">
                            {sourceData.title || 'Source'}
                        </span>
                    </div>

                    {sourceData.section_title && (
                        <p className="text-[10px] text-gray-500">
                            {sourceData.section_title}
                            {sourceData.section_number && ` (Section ${sourceData.section_number})`}
                        </p>
                    )}

                    <div className="text-xs text-gray-300 leading-relaxed max-h-60 overflow-y-auto whitespace-pre-wrap border-t border-gray-700 pt-2">
                        {sourceData.text || 'No source text available.'}
                    </div>

                    {sourceData.metadata && Object.keys(sourceData.metadata).length > 0 && (
                        <div className="text-[10px] text-gray-600 border-t border-gray-700 pt-2 space-y-0.5">
                            {Object.entries(sourceData.metadata).slice(0, 5).map(([k, v]) => (
                                <p key={k}><span className="text-gray-500">{k}:</span> {String(v)}</p>
                            ))}
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}

// ── Multi-thread chat persistence ────────────────────────────────────────
// Each thread: { id, title, messages[], createdAt, updatedAt }
// Thread index stored in georag_chat_threads (array of metadata)
// Each thread's messages stored in georag_chat_thread_{id}

function generateThreadId(): string {
    return 'thread-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 6);
}

function loadThreadIndex(): ChatThread[] {
    try {
        const raw = localStorage.getItem('georag_chat_threads');
        return raw ? JSON.parse(raw) : [];
    } catch { return []; }
}

function saveThreadIndex(threads: ChatThread[]): void {
    try {
        localStorage.setItem('georag_chat_threads', JSON.stringify(threads.slice(0, 50)));
    } catch { /* quota */ }
}

function loadThreadMessages(threadId: string): ChatMessageType[] {
    try {
        const raw = localStorage.getItem(`georag_chat_thread_${threadId}`);
        if (raw) {
            const parsed = JSON.parse(raw);
            if (Array.isArray(parsed) && parsed.length > 0) return parsed;
        }
    } catch { /* corrupt */ }
    return [WELCOME_MESSAGE];
}

function saveThreadMessages(threadId: string, msgs: ChatMessageType[]): void {
    try {
        localStorage.setItem(`georag_chat_thread_${threadId}`, JSON.stringify(msgs.slice(-50)));
    } catch { /* quota */ }
}

// ─── Server-side chat history sync ────────────────────────────────────────
// LocalStorage is the fast path. The server-side store is a durable layer
// so history survives browser-cache clear and is visible when signing in
// from a new device. Conversations are keyed by a UUID that the client
// picks; upsert is "full replace" so the client doesn't need to track
// diffs.

function toConversationUuid(threadId: string): string {
    // `thread-xxx-yyy` → deterministic UUIDv4-shaped string. Server accepts
    // any UUID-regex-matching string. We use a hash so a given threadId
    // always maps to the same conversation_id.
    let hash = 0;
    for (let i = 0; i < threadId.length; i++) {
        hash = ((hash << 5) - hash + threadId.charCodeAt(i)) & 0xffffffff;
    }
    const hex = (n: number, w: number) => (Math.abs(n) >>> 0).toString(16).padStart(w, '0').slice(0, w);
    const h = hex(hash, 8);
    // Stamp in the threadId timestamp portion for cross-device-ish uniqueness.
    const ts = threadId.split('-')[1] || Date.now().toString(36);
    const tsHex = parseInt(ts, 36).toString(16).padStart(12, '0').slice(0, 12);
    return `${h}-${tsHex.slice(0, 4)}-4${tsHex.slice(4, 7)}-a${tsHex.slice(7, 10)}-${tsHex.slice(0, 12)}`;
}

async function _authedFetch(path: string, init: RequestInit = {}): Promise<Response> {
    // Auth via Sanctum session cookie (same-origin). No bearer token from
    // localStorage — localStorage is an XSS-exfiltration target (types.ts:11-12).
    const csrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
    return fetch(path, {
        ...init,
        credentials: 'same-origin',
        headers: {
            'Content-Type': 'application/json',
            Accept: 'application/json',
            ...(csrf ? { 'X-CSRF-TOKEN': csrf } : {}),
            ...(init.headers || {}),
        },
    });
}

// Debounced sync: every push restarts a 1s timer; only the last push per
// second actually hits the server. Keeps the API quiet during rapid
// token streaming.
const _syncTimers: Record<string, ReturnType<typeof setTimeout>> = {};
function syncThreadToServer(
    threadId: string,
    title: string,
    projectId: string | null,
    msgs: ChatMessageType[],
): void {
    if (!threadId) return;
    const uuid = toConversationUuid(threadId);
    if (_syncTimers[threadId]) clearTimeout(_syncTimers[threadId]);
    _syncTimers[threadId] = setTimeout(async () => {
        try {
            await _authedFetch(`/api/v1/conversations/${uuid}`, {
                method: 'PUT',
                body: JSON.stringify({
                    title,
                    project_id: projectId,
                    messages: msgs
                        .filter((m) => m.role !== 'system' || (m.content && m.content.length > 0))
                        .map((m) => ({
                            role: m.role,
                            content: m.content ?? '',
                            metadata: {
                                citations: m.citations ?? [],
                                confidence: m.confidence ?? null,
                                map_payload: m.mapPayload ?? null,
                                viz_payload: m.vizPayload ?? null,
                            },
                        })),
                }),
            });
        } catch {
            // Network failure is non-fatal — localStorage still has the data
            // and the next update will try again.
        }
    }, 1000);
}

async function deleteThreadOnServer(threadId: string): Promise<void> {
    try {
        await _authedFetch(`/api/v1/conversations/${toConversationUuid(threadId)}`, {
            method: 'DELETE',
        });
    } catch { /* non-fatal */ }
}

function deriveThreadTitle(messages: ChatMessageType[]): string {
    const firstUser = messages.find((m) => m.role === 'user');
    if (firstUser) {
        const text = firstUser.content || '';
        return text.length > 40 ? text.slice(0, 40) + '…' : text;
    }
    return 'New conversation';
}

/**
 * R18 — build a lowercase search blob from all message bodies in the thread
 * so history search matches on body content, not just title. Truncated to
 * ~4KB per thread so the index fits in the 50-thread localStorage budget
 * without blowing the ~5MB quota.
 *
 * Welcome-message text is excluded so every thread isn't a match for
 * "drill hole" or "exploration".
 */
function deriveSearchBlob(messages: ChatMessageType[]): string {
    const parts: string[] = [];
    for (const m of messages) {
        if (m.id === 'welcome') continue;
        if (m.content) parts.push(m.content);
    }
    const blob = parts.join(' ').toLowerCase();
    return blob.length > 4096 ? blob.slice(0, 4096) : blob;
}

// Migrate old single-thread format to multi-thread
function migrateOldMessages(): ChatThread | null {
    const old = localStorage.getItem('georag_chat_messages');
    if (!old) return null;
    try {
        const msgs = JSON.parse(old);
        if (Array.isArray(msgs) && msgs.length > 1) {
            const id = generateThreadId();
            saveThreadMessages(id, msgs);
            const thread = {
                id,
                title: deriveThreadTitle(msgs),
                createdAt: Date.now(),
                updatedAt: Date.now(),
            };
            saveThreadIndex([thread]);
            localStorage.removeItem('georag_chat_messages');
            return thread;
        }
    } catch { /* ignore */ }
    return null;
}

interface ChatProps {}

export default function Chat(_props: ChatProps): JSX.Element {
    // ── Multi-thread state ───────────────────────────────────────────────
    const [threads, setThreads] = useState(() => {
        // Migrate old format if present
        const migrated = migrateOldMessages();
        const index = loadThreadIndex();
        if (migrated && index.length === 0) return [migrated];
        return index;
    });
    const [activeThreadId, setActiveThreadId] = useState(() => {
        const index = loadThreadIndex();
        return index.length > 0 ? index[0].id : null;
    });
    const [historyOpen, setHistoryOpen] = useState<boolean>(true);
    // C10 — conversation history search. Substring match on thread title
    // (case-insensitive). With the 50-thread cap this is fast enough to
    // run on every keystroke without debouncing.
    const [historySearch, setHistorySearch] = useState<string>('');

    // Create first thread if none exist
    useEffect(() => {
        if (threads.length === 0) {
            handleNewChat();
        }
    }, []);

    const [messages, setMessages] = useState(() => {
        if (activeThreadId) return loadThreadMessages(activeThreadId);
        return [WELCOME_MESSAGE];
    });
    const [inputValue, setInputValue] = useState<string>('');
    const [loading, setLoading] = useState<boolean>(false);
    const [error, setError] = useState<string | null>(null);
    const [projectId, setProjectId] = useState<string | null>(null);

    // ── Evidence inspector state ──────────────────────────────────────────
    // Single state slot drives both the new evidence-fetch path (evidenceId)
    // and the legacy SSE-only fallback (legacyCitation). Only one inspector
    // can be open at a time; the raw citation string is kept for PGEO panel
    // compatibility (CitationPGEODetail still needs the raw form).
    const [inspectorState, setInspectorState] = useState<{
        open: boolean;
        evidenceId?: string | null;
        legacyCitation?: Citation | null;
        rawCitation?: string | null;
        /** Phase H4 §12.8 — threaded so the inspector can render
         *  citation feedback (👍/👎) tied to the originating run. */
        answerRunId?: string | null;
        workspaceId?: string | null;
    }>({ open: false });

    // §19.2 Trust Inspector drawer state (per-answer rollup).
    const [trustState, setTrustState] = useState<{
        open: boolean;
        answerRunId?: string | null;
    }>({ open: false });

    // Legacy shim: activeCitation as a derived read for the PGEO panel below.
    const activeCitation = inspectorState.open ? (inspectorState.rawCitation ?? null) : null;

    // Persist messages to current thread on every update.
    // localStorage is the authoritative UI store; server sync happens in
    // the background and is debounced so rapid token updates don't hammer
    // the API.
    useEffect(() => {
        if (activeThreadId && messages.length > 1) {
            saveThreadMessages(activeThreadId, messages);
            const title = deriveThreadTitle(messages);
            // R18 — cache a lowercase search blob of every message body
            // onto the thread metadata so sidebar history search can
            // match on content, not just title.
            const search = deriveSearchBlob(messages);
            setThreads((prev) => {
                const updated = prev.map((t) =>
                    t.id === activeThreadId
                        ? { ...t, title, search, updatedAt: Date.now() }
                        : t,
                );
                saveThreadIndex(updated);
                return updated;
            });
            syncThreadToServer(activeThreadId, title, projectId, messages);
        }
    }, [messages, activeThreadId, projectId]);

    const messagesEndRef = useRef<HTMLDivElement>(null);
    const inputRef = useRef<HTMLTextAreaElement>(null);
    const echoChannelRef = useRef<{ echoChannel: EchoChannel; channel: string } | null>(null);  // for stop button
    const [stopToast, setStopToast] = useState<boolean>(false);

    // WS-01 — per-run event dedup hook. Tracks seen event_ids + lastSeq for
    // replay calls on reconnect. activeAnswerRunIdRef tracks the current run
    // so we can target the replay endpoint on reconnect.
    // hasDisconnectedRef: gates replay so it only fires on reconnect, not on
    // fresh page load.
    const activeAnswerRunIdRef = useRef<string | null>(null);
    const hasDisconnectedRef = useRef<boolean>(false);
    // V1.5-19 (STREAM-02 closure) — keep the WHOLE hook return so the
    // reconnect-replay closure can read `dedup.lastSeq` live (the hook
    // exposes lastSeq as a getter on the return object — destructuring
    // captures a snapshot, the bug being fixed here).
    const dedup = useEventDedup(activeAnswerRunIdRef.current);
    const { recordEvent, isDuplicate, reset: resetDedup } = dedup;

    // Scroll to latest message whenever messages list changes
    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [messages]);

    // Keep input focused after loading finishes
    useEffect(() => {
        if (!loading) {
            inputRef.current?.focus();
        }
    }, [loading]);

    const handleProjectChange = useCallback((id: string | null): void => {
        setProjectId(id);
    }, []);

    // D3 — clicking a follow-up chip submits it as a new user query.
    // Uses the same send path as handleSubmit; no-ops if another stream
    // is already in flight.
    const handleFollowupClick = useCallback((text: string): void => {
        if (loading || !text) return;
        setInputValue(text);
        setTimeout(() => {
            const synthetic = {
                preventDefault: () => { /* noop */ },
            } as unknown as React.FormEvent<HTMLFormElement>;
            handleSubmit(synthetic);
        }, 0);
    }, [loading]);

    // Extracted handshake: reusable for both first-send (handleSubmit) and
    // regeneration (handleRegenerate). Assumes the assistant message at
    // `assistantId` already exists and has been reset by the caller.
    async function runQueryHandshake(queryText: string, assistantId: string): Promise<void> {
        try {
            const csrfToken = document
                .querySelector('meta[name="csrf-token"]')
                ?.getAttribute('content');

            // Step 1: POST to Laravel to start the async query.
            // Auth via Sanctum session cookie (same-origin). No bearer token from
            // localStorage — localStorage is an XSS-exfiltration target (types.ts:11-12).
            const response = await fetch('/api/v1/queries', {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                    ...(csrfToken ? { 'X-CSRF-TOKEN': csrfToken } : {}),
                },
                body: JSON.stringify({
                    query: queryText,
                    project_id: projectId,
                }),
            });

            if (!response.ok) {
                let detail = 'Request failed (' + response.status + ')';
                try {
                    const errBody = await response.json();
                    if (errBody.message) detail = errBody.message;
                    else if (errBody.error) detail = errBody.error;
                } catch { /* ignore */ }
                throw new Error(detail);
            }

            const { query_id, channel } = await response.json();

            // Step 2: Subscribe to the Reverb private channel (owner-gated
            // by routes/channels.php) to receive streaming events.
            const echoChannel = window.Echo.channel(channel);
            echoChannelRef.current = { echoChannel, channel };

            let accumulatedText = '';
            let finalCitations: Citation[] = [];
            let finalConfidence = null;

            // WS-01: reset dedup state for this fresh run
            resetDedup();

            echoChannel.listen('.QueryStreamEvent', (event) => {
                // WS-01: dedup — skip events we've already applied to state.
                // This handles both live-stream duplicates (rare) and replay
                // events that arrive after a reconnect.
                if (isDuplicate(event)) return;
                recordEvent(event);

                const eventType = event.event;

                if (eventType === 'status' && event.message) {
                    // C1 — accumulate phases rather than overwriting so the
                    // user sees the full retrieval trail (Claude/Perplexity
                    // parity). The previous phase is marked done; the new
                    // phase is pushed as running. `status` still gets set
                    // for backward-compat with ChatMessage's spinner row —
                    // the new checklist uses `phases`.
                    setMessages((prev) =>
                        prev.map((msg) => {
                            if (msg.id !== assistantId) return msg;
                            const prevPhases = Array.isArray(msg.phases) ? msg.phases : [];
                            const completed = prevPhases.map((p) =>
                                p.state === 'running' ? { ...p, state: 'done' } : p
                            );
                            // Dedupe: if the new label matches the last
                            // already-running phase, don't push a second.
                            const last = completed[completed.length - 1];
                            if (last && last.label === event.message) {
                                return { ...msg, status: event.message, phases: completed };
                            }
                            return {
                                ...msg,
                                status: event.message,
                                phases: [
                                    ...completed,
                                    { label: event.message, state: 'running' },
                                ],
                            };
                        })
                    );
                } else if (eventType === 'routing') {
                    // B1 — model routing decision; surface as a subtle
                    // phase entry so the user can see "Routed to Haiku /
                    // fast tier" in the trail.
                    const label = `Routed to ${event.tier || 'unknown'} tier${event.reason === 'failover' ? ' (failover)' : ''}`;
                    setMessages((prev) =>
                        prev.map((msg) => {
                            if (msg.id !== assistantId) return msg;
                            const prevPhases = Array.isArray(msg.phases) ? msg.phases : [];
                            return {
                                ...msg,
                                phases: [
                                    ...prevPhases.map((p) =>
                                        p.state === 'running' ? { ...p, state: 'done' } : p
                                    ),
                                    { label, state: 'done', kind: 'routing' },
                                ],
                            };
                        })
                    );
                } else if (eventType === 'bind') {
                    // Eval 02 follow-up (2026-05-20) — citations-bound-pre-tokens.
                    // The orchestrator emits this BEFORE the first token arrives.
                    // The payload carries the citation manifest the answer is
                    // allowed to use. Render anchor chips immediately so the
                    // geologist never sees an unanchored answer.
                    if (Array.isArray(event.citations)) {
                        const bound = event.citations
                            .filter((c: { citation_id?: string }) => !!c.citation_id)
                            .map((c: {
                                citation_id: string;
                                kind?: string;
                                store?: string;
                                display_ref?: Record<string, unknown> | null;
                            }) => ({
                                citation_id: c.citation_id,
                                citation_type: c.kind ?? 'DATA',
                                source_chunk_id: c.citation_id,
                                document_title: undefined,
                                relevance_score: undefined,
                            }));
                        if (bound.length > 0) {
                            // Replace, don't append — the bind manifest is the
                            // canonical pre-generation set. Per-citation events
                            // arriving later will be deduped by citation_id in
                            // the merging step below.
                            finalCitations = bound;
                            setMessages((prev) =>
                                prev.map((msg) =>
                                    msg.id === assistantId
                                        ? { ...msg, citations: bound }
                                        : msg
                                )
                            );
                        }
                    }
                } else if (eventType === 'delta' && event.token) {
                    accumulatedText += event.token;
                    setMessages((prev) =>
                        prev.map((msg) =>
                            msg.id === assistantId
                                ? {
                                      ...msg,
                                      content: accumulatedText,
                                      // §B2: first delta sets lifecycle to 'draft'; idempotent on subsequent deltas
                                      lifecycle_state: msg.lifecycle_state === 'draft' ? 'draft' : 'draft',
                                  }
                                : msg
                        )
                    );
                } else if (eventType === 'citation') {
                    finalCitations.push({
                        citation_id: event.citation_id,
                        citation_type: event.citation_type,
                        source_chunk_id: event.source_chunk_id,
                        document_title: event.document_title,
                        relevance_score: event.relevance_score,
                    });
                } else if (eventType === 'completed') {
                    finalConfidence = event.confidence;
                    if (event.citations) finalCitations = event.citations;
                    if (event.text) accumulatedText = event.text;

                    // M2 P5: visualization payloads ride on the completed event.
                    // Backend: src/fastapi/app/agent/viz_builder.py.
                    const finalMapPayload = event.map_payload ?? null;
                    const finalVizPayload = event.viz_payload ?? null;

                    // §B7 — extract refusal payload from completed event (Module 6 Chunk 4a shape)
                    const refusalPayload: RefusalPayload | null = event.refusal_payload ?? null;
                    const isRefusal = refusalPayload != null;

                    // §B8 — conflict detection and freshness metadata (Module 6 Phase B Chunk 4b)
                    const conflictingEvidence = Array.isArray(event.conflicting_evidence) && event.conflicting_evidence.length > 0
                        ? event.conflicting_evidence
                        : null;
                    const freshness = event.freshness ?? null;

                    // §B6 — capture answer_run_id for the feedback POST endpoint.
                    // The EventStamper attaches answer_run_id to every SSE frame.
                    const answerRunId: string | null = event.answer_run_id ?? null;
                    // Track the active run ID on the ref so reconnect logic can target it.
                    activeAnswerRunIdRef.current = answerRunId;

                    // §B2 — lifecycle: 'generated' briefly then transition to 'validated' → 'committed'
                    // (or 'rejected' if refusal_payload is present).
                    // First set 'generated' so the "Validating…" badge flashes.
                    setMessages((prev) =>
                        prev.map((msg) => {
                            if (msg.id !== assistantId) return msg;
                            const doneFinalPhases = Array.isArray(msg.phases)
                                ? msg.phases.map((p) =>
                                      p.state === 'running' ? { ...p, state: 'done' } : p
                                  )
                                : [];
                            return {
                                ...msg,
                                content: accumulatedText,
                                confidence: finalConfidence,
                                citations: finalCitations,
                                mapPayload: finalMapPayload,
                                vizPayload: finalVizPayload,
                                degradedSources: event.degraded_sources || [],
                                followups: Array.isArray(event.followups) ? event.followups : [],
                                status: null,
                                error: null,
                                phases: doneFinalPhases,
                                refusal_payload: refusalPayload,
                                // §B8 — conflict + freshness fields
                                conflicting_evidence: conflictingEvidence,
                                freshness,
                                // §B6 — answer_run_id for feedback POST
                                answer_run_id: answerRunId,
                                // Transient 'generated' state while we schedule the visual transition
                                lifecycle_state: 'generated',
                            };
                        })
                    );

                    // §B2 — after ~300ms, transition to 'validated' (or 'rejected')
                    setTimeout(() => {
                        setMessages((prev) =>
                            prev.map((msg) =>
                                msg.id === assistantId
                                    ? {
                                          ...msg,
                                          lifecycle_state: isRefusal ? 'rejected' : 'validated',
                                      }
                                    : msg
                            )
                        );

                        // §B2 — after another ~500ms, 'committed' (only for non-refusal paths)
                        if (!isRefusal) {
                            setTimeout(() => {
                                setMessages((prev) =>
                                    prev.map((msg) =>
                                        msg.id === assistantId && msg.lifecycle_state === 'validated'
                                            ? { ...msg, lifecycle_state: 'committed' }
                                            : msg
                                    )
                                );
                            }, 500);
                        }
                    }, 300);

                    // Cleanup: stop listening and leave the channel
                    echoChannel.stopListening('.QueryStreamEvent');
                    window.Echo.leave(channel);
                    setLoading(false);
                    // WS-01: cleanup reconnect listeners
                    (window as any).__georag_reconnect_cleanup?.();
                } else if (eventType === 'failed' || eventType === 'error') {
                    const errorMsg = event.error || event.message || 'Query failed';
                    const errorCode = event.code || null;
                    setError(errorMsg);

                    // §B7 — synthesise a minimal RefusalPayload for system failures so
                    // RefusalPanel can render even when we don't have real searched/missing data.
                    const syntheticRefusal: RefusalPayload = {
                        type: 'refusal',
                        reason_code: errorCode === 'TIMEOUT' ? 'budget_exhausted' : 'llm_unavailable',
                        searched: {
                            stores_queried: [],
                            candidates_considered: 0,
                            query_class: 'unknown',
                        },
                        missing: {
                            what_was_needed: 'An answer within the time budget',
                            nearest_candidates: [],
                        },
                        message: errorMsg,
                    };

                    setMessages((prev) =>
                        prev.map((msg) =>
                            msg.id === assistantId
                                ? {
                                      ...msg,
                                      content: `Error: ${errorMsg}`,
                                      error: errorMsg,
                                      status: null,
                                      // §B2 lifecycle + §B7 refusal payload
                                      lifecycle_state: 'rejected',
                                      refusal_payload: syntheticRefusal,
                                  }
                                : msg
                        )
                    );
                    echoChannel.stopListening('.QueryStreamEvent');
                    window.Echo.leave(channel);
                    setLoading(false);
                    // WS-01: cleanup reconnect listeners
                    (window as any).__georag_reconnect_cleanup?.();
                }
            });

            // Phase 2 of the subscribe-ACK handshake: tell Laravel to
            // dispatch the Horizon job now that our listener is attached.
            // We call this *after* `.listen()` above so the listener is
            // registered on the Echo channel object; pusher-js keeps a
            // queue of listeners and flushes them as soon as the server
            // confirms the subscription, so we don't need an explicit
            // subscription_succeeded callback here.
            //
            // If `/start` returns 409 the server is telling us the job
            // was already dispatched (double-click, rapid retry); that's
            // idempotent — we continue listening since the broadcast
            // will still land on our channel.
            try {
                const startResp = await fetch(`/api/v1/queries/${query_id}/start`, {
                    method: 'POST',
                    headers: {
                        'Accept': 'application/json',
                        'X-Requested-With': 'XMLHttpRequest',
                        ...(csrfToken ? { 'X-CSRF-TOKEN': csrfToken } : {}),
                    },
                });
                if (!startResp.ok && startResp.status !== 409) {
                    const detail = await startResp.text();
                    throw new Error(`Failed to start query (${startResp.status}): ${detail.slice(0, 200)}`);
                }
            } catch (startErr) {
                echoChannel.stopListening('.QueryStreamEvent');
                window.Echo.leave(channel);
                throw startErr;
            }

            // Safety timeout — if no completed event within 5 min, give up.
            setTimeout(() => {
                if (loading) {
                    echoChannel.stopListening('.QueryStreamEvent');
                    window.Echo.leave(channel);
                    setLoading(false);
                    setError('Query timed out waiting for response');
                    setMessages((prev) =>
                        prev.map((msg) =>
                            msg.id === assistantId && !msg.confidence
                                ? { ...msg, error: 'Timed out waiting for response', status: null }
                                : msg
                        )
                    );
                }
            }, 300_000);

            // WS-01 — Reconnect / visibility-change replay.
            // When the browser tab is hidden and the WebSocket drops, we catch
            // up on missed events by fetching from the Redis ring buffer.
            // Gate with hasDisconnectedRef so we only replay on actual reconnect,
            // not on fresh page load. Dedup via the useEventDedup hook above.
            async function replayMissedEvents() {
                const runId = activeAnswerRunIdRef.current;
                if (!runId) return;
                // V1.5-19 (STREAM-02) — read `lastSeq` LIVE from the hook
                // every call. `dedup.lastSeq` is a getter on the
                // `useEventDedup` return object and reflects the current
                // value of the underlying ref, so the closure here always
                // sees the most recent event_seq applied. Previous
                // implementation used a stale `lastSeqRef.current` placeholder
                // initialised to 0, which broke catchup when reconnecting
                // after the stream had already received events.
                const lastSeqLocal = dedup.lastSeq;
                // /fastapi/* is a same-origin infra proxy (nginx → FastAPI container).
                // Auth via Sanctum session cookie. No bearer token from localStorage —
                // localStorage is an XSS-exfiltration target (types.ts:11-12).
                const csrfT = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
                try {
                    const resp = await fetch(
                        `/fastapi/v1/answer_runs/${runId}/events?since_event_seq=${lastSeqLocal}`,
                        {
                            credentials: 'same-origin',
                            headers: {
                                'Accept': 'application/json',
                                ...(csrfT ? { 'X-CSRF-TOKEN': csrfT } : {}),
                            },
                        }
                    );
                    if (!resp.ok) return;
                    const events: Record<string, unknown>[] = await resp.json();
                    events.forEach((event) => {
                        if (isDuplicate(event)) return;
                        recordEvent(event);
                        // Re-dispatch into the same handler by simulating the event.
                        // We use the same dispatch logic path — treat this as a live event.
                        const eventType = event.event;
                        if (eventType === 'delta' && event.token) {
                            setMessages((prev) =>
                                prev.map((msg) =>
                                    msg.id === assistantId
                                        ? { ...msg, content: (msg.content ?? '') + event.token }
                                        : msg
                                )
                            );
                        }
                        // Other event types (completed, failed, citation) are handled
                        // by the fact that the completed event from replay will include
                        // the full accumulated text — no partial-token replay needed.
                    });
                } catch { /* silent — replay is best-effort */ }
            }

            // V1.5-19 — removed the previous bogus `lastSeqRef = { current: 0 }`
            // placeholder + `_originalRecordEvent` override. `replayMissedEvents`
            // now reads `dedup.lastSeq` directly via the hook's live getter.

            function handleVisibilityChange() {
                if (document.visibilityState === 'visible' && hasDisconnectedRef.current) {
                    replayMissedEvents();
                }
            }
            function handleEchoDisconnect() {
                hasDisconnectedRef.current = true;
            }

            document.addEventListener('visibilitychange', handleVisibilityChange);
            // Best-effort Echo error detection (pusher-js may emit via connector)
            if (window.Echo?.connector?.pusher) {
                window.Echo.connector.pusher.connection.bind('error', handleEchoDisconnect);
                window.Echo.connector.pusher.connection.bind('disconnected', handleEchoDisconnect);
            }

            // Cleanup listeners when the handshake function scope ends (on completed/failed)
            // We do this by adding to the echoChannel cleanup path.
            const _cleanupReconnect = () => {
                document.removeEventListener('visibilitychange', handleVisibilityChange);
                if (window.Echo?.connector?.pusher) {
                    window.Echo.connector.pusher.connection.unbind('error', handleEchoDisconnect);
                    window.Echo.connector.pusher.connection.unbind('disconnected', handleEchoDisconnect);
                }
            };
            // Store cleanup on window temporarily so the SSE handler can call it
            (window as any).__georag_reconnect_cleanup = _cleanupReconnect;

        } catch (err) {
            const errorMsg = (err instanceof Error ? err.message : null) ?? 'An unexpected error occurred.';
            setError(errorMsg);
            setMessages((prev) =>
                prev.map((msg) =>
                    msg.id === assistantId
                        ? {
                              ...msg,
                              content: `Sorry, I was unable to process that request. (${errorMsg})`,
                              error: errorMsg,
                              status: null,
                          }
                        : msg
                )
            );
            setLoading(false);
        }
    }

    async function handleSubmit(e: React.FormEvent<HTMLFormElement>): Promise<void> {
        e.preventDefault();

        const trimmed = inputValue.trim();
        if (!trimmed || loading) return;

        const userMessage: ChatMessageType = {
            id: nextId(),
            role: 'user',
            content: trimmed,
            timestamp: new Date().toISOString(),
        };

        // Create a placeholder assistant message that we'll fill in as tokens stream.
        // `originalQuery` is persisted on the assistant message itself so that
        // Regenerate can recover the prompt even if the thread was loaded from
        // the server-synced shape where the user message is trimmed.
        const assistantId = nextId();
        const assistantMessage: ChatMessageType = {
            id: assistantId,
            role: 'assistant',
            content: '',
            confidence: null,
            citations: [],
            mapPayload: null,
            vizPayload: null,
            originalQuery: trimmed,
            error: null,
            timestamp: new Date().toISOString(),
        };

        setMessages((prev) => [...prev, userMessage, assistantMessage]);
        setInputValue('');
        setLoading(true);
        setError(null);

        await runQueryHandshake(trimmed, assistantId);
    }

    // Regenerate: replay the query that produced `assistantMessageId` and
    // replace the assistant bubble in place so thread length is stable.
    // No-op while another stream is already running (single-stream guard
    // matches the existing handleStopGenerating assumption).
    async function handleRegenerate(assistantMessageId: string): Promise<void> {
        if (loading) return;

        // Locate the user message that produced this assistant response by
        // walking the thread backward. Fall back to the persisted
        // originalQuery on the assistant message if no user message is
        // present (covers older synced thread shapes).
        let queryText: string | null = null;
        const idx = messages.findIndex((m) => m.id === assistantMessageId);
        if (idx === -1) return;
        for (let i = idx - 1; i >= 0; i--) {
            if (messages[i].role === 'user' && messages[i].content) {
                queryText = messages[i].content;
                break;
            }
        }
        if (!queryText) {
            const assistantMsg = messages.find((m) => m.id === assistantMessageId);
            queryText = assistantMsg?.originalQuery ?? null;
        }
        if (!queryText) return;

        // Reset the assistant bubble in place — same id, cleared state.
        const freshQuery = queryText;
        setMessages((prev) =>
            prev.map((m) =>
                m.id === assistantMessageId
                    ? {
                          ...m,
                          content: '',
                          confidence: null,
                          citations: [],
                          mapPayload: null,
                          vizPayload: null,
                          status: null,
                          error: null,
                          originalQuery: freshQuery,
                          timestamp: new Date().toISOString(),
                      }
                    : m
            )
        );
        setLoading(true);
        setError(null);

        await runQueryHandshake(freshQuery, assistantMessageId);
    }

    function handleStopGenerating(): void {
        if (echoChannelRef.current) {
            const { echoChannel, channel } = echoChannelRef.current;
            echoChannel.stopListening('.QueryStreamEvent');
            window.Echo.leave(channel);
            echoChannelRef.current = null;
        }
        setLoading(false);
        setStopToast(true);
        setTimeout(() => setStopToast(false), 3000);
    }

    function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>): void {
        // Submit on Enter; allow Shift+Enter for newlines
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSubmit(e as unknown as import('react').FormEvent<HTMLFormElement>);
        }
    }

    function handleCitationClick(citationRaw: string): void {
        // Find citation data from the most recent assistant message's citation cache.
        const lastAssistant = [...messages].reverse().find(
            (m) => m.role === 'assistant' && Array.isArray(m.citations) && m.citations.length > 0
        );
        const citData: Citation | undefined = lastAssistant?.citations?.find(
            (c) => c.citation_id === citationRaw
        );

        // If the citation carries an evidence_id (Module 6+), open the
        // full inspector. Otherwise fall back to the legacy SSE-payload display.
        const evidenceId: string | null = citData?.evidence_id ?? null;

        // Thread answer_run_id + workspace_id so the inspector can render
        // §12.8 citation feedback (👍/👎) tied to the originating run.
        const answerRunId: string | null =
            (lastAssistant as { answer_run_id?: string | null } | undefined)?.answer_run_id ?? null;
        const workspaceId: string | null =
            (typeof window !== 'undefined'
                ? window.localStorage.getItem('georag_workspace_id')
                : null) ?? 'a0000000-0000-0000-0000-000000000001';

        setInspectorState({
            open: true,
            evidenceId: evidenceId ?? null,
            legacyCitation: citData ?? null,
            rawCitation: citationRaw,
            answerRunId,
            workspaceId,
        });
    }

    function handleNewChat(): void {
        const id = generateThreadId();
        const newThread = { id, title: 'New conversation', createdAt: Date.now(), updatedAt: Date.now() };
        const updated = [newThread, ...threads];
        setThreads(updated);
        saveThreadIndex(updated);
        setActiveThreadId(id);
        setMessages([WELCOME_MESSAGE]);
        saveThreadMessages(id, [WELCOME_MESSAGE]);
        setInspectorState({ open: false });
        setHistoryOpen(false);
    }

    function handleSwitchThread(threadId: string): void {
        if (threadId === activeThreadId) return;
        setActiveThreadId(threadId);
        setMessages(loadThreadMessages(threadId));
        setInspectorState({ open: false });
        setHistoryOpen(false);
    }

    function handleDeleteThread(threadId: string): void {
        const updated = threads.filter((t) => t.id !== threadId);
        setThreads(updated);
        saveThreadIndex(updated);
        localStorage.removeItem(`georag_chat_thread_${threadId}`);
        // Propagate delete to server. Fire-and-forget — localStorage is
        // already the single source of truth for the UI.
        deleteThreadOnServer(threadId);
        if (threadId === activeThreadId) {
            if (updated.length > 0) {
                handleSwitchThread(updated[0].id);
            } else {
                handleNewChat();
            }
        }
    }

    function handleClearConversation(): void {
        if (activeThreadId && window.confirm('Delete this conversation? This cannot be undone.')) {
            handleDeleteThread(activeThreadId);
        }
    }

    function handleExportConversation(): void {
        const md = messages
            .filter((m) => m.id !== 'welcome')
            .map((m) => `**${m.role === 'user' ? 'You' : 'GeoRAG'}:** ${m.content}`)
            .join('\n\n---\n\n');

        const blob = new Blob([md], { type: 'text/markdown' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `georag-chat-${new Date().toISOString().slice(0, 10)}.md`;
        a.click();
        URL.revokeObjectURL(url);
    }

    function closeCitationPanel(): void {
        setInspectorState({ open: false });
    }

    return (
        <AppLayout onProjectChange={handleProjectChange}>
            <Head title="Chat" />

            <div className="flex flex-1 overflow-hidden h-full">

                {/* ── Chat history sidebar (always visible, sticky) ── */}
                <aside className="w-64 shrink-0 border-r border-gray-800 bg-gray-900 flex flex-col sticky top-0 self-start" style={{ maxHeight: 'calc(100vh - 56px)' }}>
                    <div className="flex items-center justify-between px-3 py-3 border-b border-gray-800 shrink-0">
                        <h2 className="text-xs font-semibold text-gray-300 uppercase tracking-wider">History</h2>
                        <span className="text-[10px] text-gray-600">
                            {threads.length} chat{threads.length !== 1 ? 's' : ''}
                        </span>
                    </div>

                        {/* New chat button */}
                        <button
                            type="button"
                            onClick={handleNewChat}
                            className="mx-3 mt-3 mb-2 flex items-center gap-2 text-xs text-amber-400 hover:text-amber-300 border border-amber-800/50 hover:border-amber-600/50 bg-amber-950/30 rounded-lg px-3 py-2 transition-colors"
                        >
                            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" className="w-3.5 h-3.5">
                                <path fillRule="evenodd" d="M12 3.75a.75.75 0 0 1 .75.75v6.75h6.75a.75.75 0 0 1 0 1.5h-6.75v6.75a.75.75 0 0 1-1.5 0v-6.75H4.5a.75.75 0 0 1 0-1.5h6.75V4.5a.75.75 0 0 1 .75-.75Z" clipRule="evenodd" />
                            </svg>
                            New Chat
                        </button>

                        {/* C10 — history search. Visible only once there's
                            at least three threads; below that, scrolling is
                            cheap and the field is noise. */}
                        {threads.length >= 3 && (
                            <div className="mx-3 mb-2">
                                <label htmlFor="history-search" className="sr-only">Search conversations</label>
                                <input
                                    id="history-search"
                                    type="search"
                                    value={historySearch}
                                    onChange={(e) => setHistorySearch(e.target.value)}
                                    placeholder="Search…"
                                    className="w-full text-xs bg-gray-800 border border-gray-700 rounded-lg px-2.5 py-1.5 text-gray-200 placeholder-gray-500 focus:outline-none focus:ring-1 focus:ring-amber-500 focus:border-transparent"
                                />
                            </div>
                        )}

                        {/* Thread list */}
                        <div className="flex-1 overflow-y-auto px-2">
                            {threads
                                .map((thread) => {
                                    // R18 — annotate each thread with its
                                    // search match state (title / body / none)
                                    // before filtering so the list can show a
                                    // "matched in body" hint for body-only hits.
                                    const q = historySearch.trim().toLowerCase();
                                    if (!q) return { thread, matched: 'all', bodyOnly: false };
                                    const titleMatch = (thread.title || '').toLowerCase().includes(q);
                                    const bodyMatch = (thread.search || '').includes(q);
                                    if (!titleMatch && !bodyMatch) return null;
                                    return {
                                        thread,
                                        matched: titleMatch ? 'title' : 'body',
                                        bodyOnly: !titleMatch && bodyMatch,
                                    };
                                })
                                .filter((x) => x !== null)
                                .sort((a, b) => (Number(b.thread.updatedAt) || 0) - (Number(a.thread.updatedAt) || 0))
                                .map(({ thread, bodyOnly }) => (
                                    <div
                                        key={thread.id}
                                        className={`group flex items-center gap-1 rounded-lg px-2.5 py-2 mb-0.5 cursor-pointer transition-colors ${
                                            thread.id === activeThreadId
                                                ? 'bg-amber-950/40 border border-amber-800/40'
                                                : 'hover:bg-gray-800 border border-transparent'
                                        }`}
                                        onClick={() => handleSwitchThread(thread.id)}
                                    >
                                        <div className="flex-1 min-w-0">
                                            <p className={`text-xs truncate ${
                                                thread.id === activeThreadId ? 'text-amber-300' : 'text-gray-300'
                                            }`}>
                                                {thread.title || 'New conversation'}
                                            </p>
                                            <p className="text-[10px] text-gray-500 flex items-center gap-1.5">
                                                {thread.updatedAt
                                                    ? new Date(thread.updatedAt).toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
                                                    : ''}
                                                {/* R18 — indicate that the match was on body
                                                    content, not title, so the user knows why a
                                                    thread with an unrelated-looking title is
                                                    appearing. */}
                                                {bodyOnly && (
                                                    <span className="inline-flex items-center px-1 py-0.5 rounded bg-amber-950/60 border border-amber-800/50 text-amber-400 text-[9px] uppercase tracking-wide">
                                                        in body
                                                    </span>
                                                )}
                                            </p>
                                        </div>
                                        <button
                                            type="button"
                                            onClick={(e: React.MouseEvent<HTMLButtonElement>) => { e.stopPropagation(); handleDeleteThread(thread.id); }}
                                            className="opacity-0 group-hover:opacity-100 text-gray-500 hover:text-red-400 p-0.5 transition-opacity"
                                            aria-label="Delete conversation"
                                        >
                                            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" className="w-3 h-3">
                                                <path fillRule="evenodd" d="M16.5 4.478v.227a48.816 48.816 0 0 1 3.878.512.75.75 0 1 1-.256 1.478l-.209-.035-1.005 13.07a3 3 0 0 1-2.991 2.77H8.084a3 3 0 0 1-2.991-2.77L4.087 6.66l-.209.035a.75.75 0 0 1-.256-1.478A48.567 48.567 0 0 1 7.5 4.705v-.227c0-1.564 1.213-2.9 2.816-2.951a52.662 52.662 0 0 1 3.369 0c1.603.051 2.815 1.387 2.815 2.951Zm-6.136-1.452a51.196 51.196 0 0 1 3.273 0C14.39 3.05 15 3.684 15 4.478v.113a49.488 49.488 0 0 0-6 0v-.113c0-.794.609-1.428 1.364-1.452Zm-.355 5.945a.75.75 0 1 0-1.5.058l.347 9a.75.75 0 1 0 1.499-.058l-.346-9Zm5.48.058a.75.75 0 1 0-1.498-.058l-.347 9a.75.75 0 0 0 1.5.058l.345-9Z" clipRule="evenodd" />
                                            </svg>
                                        </button>
                                    </div>
                                ))}
                            {/* C10 + R18 — empty-search hint. Search now
                                covers both title and message body. */}
                            {historySearch.trim() && (() => {
                                const q = historySearch.trim().toLowerCase();
                                const anyMatch = threads.some(
                                    (t) => (t.title || '').toLowerCase().includes(q)
                                        || (t.search || '').includes(q),
                                );
                                if (anyMatch) return null;
                                return (
                                    <p className="text-xs text-gray-400 px-3 py-4 text-center">
                                        No conversations match "<span className="font-mono">{historySearch}</span>" in title or body.
                                    </p>
                                );
                            })()}
                        </div>
                </aside>

                {/* ── Message area ── */}
                <div className="flex flex-col flex-1 overflow-hidden">

                    {/* Scrollable message list */}
                    <div
                        className="flex-1 overflow-y-auto px-4 py-6"
                        role="log"
                        aria-label="Conversation"
                        aria-live="polite"
                        aria-relevant="additions"
                    >
                        <div className="max-w-3xl mx-auto">
                            {messages.map((msg) => (
                                <ChatMessage
                                    key={msg.id}
                                    message={msg}
                                    projectId={projectId}
                                    onCitationClick={handleCitationClick}
                                    onRegenerate={handleRegenerate}
                                    onFollowupClick={handleFollowupClick}
                                    onInspectCandidate={(marker, evidenceId, legacyCitation) => {
                                        // Route nearest-candidate clicks from RefusalPanel
                                        // to the same EvidenceInspector used for citation clicks.
                                        setInspectorState({
                                            open: true,
                                            evidenceId: evidenceId ?? null,
                                            legacyCitation: legacyCitation ?? null,
                                            rawCitation: marker,
                                        });
                                    }}
                                    isStreaming={loading}
                                />
                            ))}

                            {/* C3 — empty-state prompt chips. Rendered only
                                when the thread contains just the welcome
                                message; clicking one submits the query
                                directly so the user never has to type the
                                first question. */}
                            {!loading && messages.length === 1 && messages[0].id === 'welcome' && (
                                <div className="mt-4 mb-2">
                                    <p className="text-xs text-gray-400 mb-2.5 uppercase tracking-wide">
                                        Try one of these
                                    </p>
                                    <div className="flex flex-wrap gap-2">
                                        {EMPTY_STATE_PROMPTS.map((p) => (
                                            <button
                                                key={p.query}
                                                type="button"
                                                onClick={() => {
                                                    // Fire the same path as the form submit.
                                                    setInputValue(p.query);
                                                    // Defer submission one tick so setInputValue
                                                    // has flushed before handleSubmit reads it.
                                                    setTimeout(() => {
                                                        const synthetic = {
                                                            preventDefault: () => { /* noop */ },
                                                        } as unknown as React.FormEvent<HTMLFormElement>;
                                                        handleSubmit(synthetic);
                                                    }, 0);
                                                }}
                                                className="text-left text-sm text-gray-200 bg-gray-800/60 hover:bg-gray-800 border border-gray-700 hover:border-amber-700 rounded-lg px-3 py-2 transition-colors focus:outline-none focus:ring-2 focus:ring-amber-500 max-w-sm"
                                                aria-label={`Send example query: ${p.label}`}
                                            >
                                                {p.label}
                                            </button>
                                        ))}
                                    </div>
                                </div>
                            )}

                            {/* Loading indicator with stop button */}
                            {loading && (
                                <div className="flex justify-start mb-4" aria-live="polite" aria-label="GeoRAG is thinking">
                                    <div className="w-7 h-7 rounded-full bg-amber-700 flex items-center justify-center text-xs font-bold text-amber-100 shrink-0 mt-0.5 mr-2">
                                        G
                                    </div>
                                    <div className="flex items-center gap-3">
                                        <div className="bg-gray-800 border border-gray-700 rounded-2xl rounded-bl-sm px-4 py-3">
                                            <div className="flex items-center gap-1.5">
                                                <span className="w-2 h-2 rounded-full bg-gray-500 animate-bounce motion-reduce:animate-none" style={{ animationDelay: '0ms' }} />
                                                <span className="w-2 h-2 rounded-full bg-gray-500 animate-bounce motion-reduce:animate-none" style={{ animationDelay: '150ms' }} />
                                                <span className="w-2 h-2 rounded-full bg-gray-500 animate-bounce motion-reduce:animate-none" style={{ animationDelay: '300ms' }} />
                                            </div>
                                        </div>
                                        <button
                                            type="button"
                                            onClick={handleStopGenerating}
                                            className="text-xs text-gray-500 hover:text-red-400 border border-gray-700 hover:border-red-700 rounded-lg px-2.5 py-1.5 transition-colors"
                                            aria-label="Stop generating"
                                        >
                                            Stop
                                        </button>
                                    </div>
                                </div>
                            )}

                            {/* Stop toast */}
                            {stopToast && (
                                <div className="flex justify-center mb-2">
                                    <span className="text-xs text-gray-500 bg-gray-800 border border-gray-700 rounded-full px-3 py-1">
                                        Generation stopped
                                    </span>
                                </div>
                            )}

                            {/* Scroll anchor */}
                            <div ref={messagesEndRef} />
                        </div>
                    </div>

                    {/* ── Input bar ── */}
                    <div className="shrink-0 border-t border-gray-800 bg-gray-900 px-4 py-4">
                        {/* D6 — project context banner (name · commodity · region · CRS · hole count) */}
                        <ProjectContextBanner projectId={projectId} />

                        {/* Inline error banner */}
                        {error && (
                            <div
                                className="max-w-3xl mx-auto mb-3 flex items-start gap-2 bg-red-950/60 border border-red-800/50 text-red-300 text-sm rounded-lg px-4 py-2.5"
                                role="alert"
                            >
                                <span className="shrink-0 mt-0.5 text-red-400" aria-hidden="true">!</span>
                                <span className="flex-1">{error}</span>
                                <button
                                    type="button"
                                    onClick={() => setError(null)}
                                    className="shrink-0 text-red-400 hover:text-red-200 rounded focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-offset-1 focus:ring-offset-red-950"
                                    aria-label="Dismiss error"
                                >
                                    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" className="w-4 h-4" aria-hidden="true">
                                        <path fillRule="evenodd" d="M5.47 5.47a.75.75 0 0 1 1.06 0L12 10.94l5.47-5.47a.75.75 0 1 1 1.06 1.06L13.06 12l5.47 5.47a.75.75 0 1 1-1.06 1.06L12 13.06l-5.47 5.47a.75.75 0 0 1-1.06-1.06L10.94 12 5.47 6.53a.75.75 0 0 1 0-1.06Z" clipRule="evenodd" />
                                    </svg>
                                </button>
                            </div>
                        )}

                        <form
                            onSubmit={handleSubmit}
                            className="max-w-3xl mx-auto flex gap-3 items-end"
                        >
                            <div className="flex-1 relative">
                                <textarea
                                    ref={inputRef}
                                    value={inputValue}
                                    onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => setInputValue(e.target.value.slice(0, 1000))}
                                    onKeyDown={handleKeyDown}
                                    placeholder="Ask about drill holes, lithology, grades, reports… (Enter to send, Shift+Enter for newline)"
                                    rows={1}
                                    maxLength={1000}
                                    disabled={loading}
                                    aria-label="Query input — ask a geological question"
                                    className={[
                                        'w-full resize-none',
                                        'bg-gray-800 text-gray-100 placeholder-gray-500',
                                        'border border-gray-700 rounded-xl',
                                        'px-4 py-3 pr-12',
                                        'text-sm leading-relaxed',
                                        'focus:outline-none focus:ring-2 focus:ring-amber-500 focus:border-transparent',
                                        'disabled:opacity-50 disabled:cursor-not-allowed',
                                        'max-h-48 overflow-y-auto',
                                        'transition-colors duration-150',
                                    ].join(' ')}
                                    style={{
                                        // Auto-grow textarea up to max-h-48
                                        height: 'auto',
                                        minHeight: '48px',
                                    }}
                                    onInput={(e: React.FormEvent<HTMLTextAreaElement>) => {
                                        // Auto-grow
                                        (e.target as HTMLTextAreaElement).style.height = 'auto';
                                        (e.target as HTMLTextAreaElement).style.height = (e.target as HTMLTextAreaElement).scrollHeight + 'px';
                                    }}
                                />
                            </div>

                            {/* Send button */}
                            <button
                                type="submit"
                                disabled={loading || !inputValue.trim()}
                                aria-label="Send message"
                                className={[
                                    'shrink-0',
                                    'w-11 h-11',
                                    'rounded-xl',
                                    'flex items-center justify-center',
                                    'font-medium text-sm',
                                    'transition-colors duration-150',
                                    'focus:outline-none focus:ring-2 focus:ring-amber-500 focus:ring-offset-2 focus:ring-offset-gray-900',
                                    loading || !inputValue.trim()
                                        ? 'bg-gray-700 text-gray-500 cursor-not-allowed'
                                        : 'bg-amber-600 hover:bg-amber-500 text-white cursor-pointer',
                                ].join(' ')}
                            >
                                {/* Arrow icon — pure SVG, no dependency */}
                                <svg
                                    xmlns="http://www.w3.org/2000/svg"
                                    viewBox="0 0 24 24"
                                    fill="currentColor"
                                    className="w-5 h-5"
                                    aria-hidden="true"
                                >
                                    <path d="M3.478 2.405a.75.75 0 0 0-.926.94l2.432 7.905H13.5a.75.75 0 0 1 0 1.5H4.984l-2.432 7.905a.75.75 0 0 0 .926.94 60.519 60.519 0 0 0 18.445-8.986.75.75 0 0 0 0-1.218A60.517 60.517 0 0 0 3.478 2.405Z" />
                                </svg>
                            </button>
                        </form>

                        {/* Footer hint */}
                        <div className="max-w-3xl mx-auto mt-2 flex items-center justify-between text-xs text-gray-600">
                            <p className="text-center flex-1">
                                GeoRAG answers are AI-generated. Always verify against source documents.
                            </p>
                            <div className="flex gap-2 shrink-0 ml-3">
                                <button
                                    type="button"
                                    onClick={handleExportConversation}
                                    className="text-gray-400 hover:text-gray-200 transition-colors px-1 rounded focus:outline-none focus:ring-2 focus:ring-amber-500 focus:ring-offset-1 focus:ring-offset-gray-900"
                                    title="Export conversation as Markdown"
                                    aria-label="Export conversation as Markdown"
                                >
                                    Export
                                </button>
                                <button
                                    type="button"
                                    onClick={handleClearConversation}
                                    className="text-gray-400 hover:text-red-400 transition-colors px-1 rounded focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-offset-1 focus:ring-offset-gray-900"
                                    title="Clear conversation"
                                    aria-label="Clear conversation"
                                >
                                    Clear
                                </button>
                            </div>
                        </div>
                    </div>
                </div>

                {/* ── Evidence Inspector Sheet (INSP-01) ── */}
                {/* Replaces the old 320px inline side-panel. Renders as a
                    shadcn Sheet overlay so it doesn't compress the chat layout.
                    On PGEO citations the legacy CitationPGEODetail content is
                    passed as legacyCitation (EvidenceInspector renders it via
                    the LegacyCitationRenderer until evidence_items land). */}
                <EvidenceInspector
                    open={inspectorState.open}
                    onOpenChange={(open) => {
                        if (!open) closeCitationPanel();
                    }}
                    evidenceId={inspectorState.evidenceId ?? null}
                    legacyCitation={inspectorState.legacyCitation ?? null}
                    answerRunId={inspectorState.answerRunId ?? null}
                    workspaceId={inspectorState.workspaceId ?? null}
                />

                {/* §19.2 Trust Inspector — floating button on latest answer */}
                {(() => {
                    const lastAssistantWithRun = [...messages].reverse().find(
                        (m) => m.role === 'assistant' && (m as { answer_run_id?: string | null }).answer_run_id,
                    );
                    const arid = (lastAssistantWithRun as { answer_run_id?: string | null } | undefined)?.answer_run_id;
                    if (!arid) return null;
                    return (
                        <button
                            type="button"
                            onClick={() => setTrustState({ open: true, answerRunId: arid })}
                            className="fixed bottom-24 right-6 z-40 inline-flex items-center gap-2 rounded-full bg-indigo-600 px-4 py-2 text-sm font-medium text-white shadow-lg hover:bg-indigo-500"
                            aria-label="Open Trust Inspector"
                        >
                            <span aria-hidden="true">🛡️</span>
                            Trust
                        </button>
                    );
                })()}

                <TrustInspector
                    open={trustState.open}
                    onClose={() => setTrustState({ open: false })}
                    answerRunId={trustState.answerRunId ?? null}
                    projectId={projectId}
                    onOpenEvidence={(eid) => {
                        setTrustState({ open: false });
                        setInspectorState({
                            open: true,
                            evidenceId: eid,
                            legacyCitation: null,
                            rawCitation: null,
                            answerRunId: trustState.answerRunId ?? null,
                        });
                    }}
                />
            </div>
        </AppLayout>
    );
}
