import { useState, useEffect, useRef, useCallback } from 'react';
import type { FormEvent, ReactNode } from 'react';

/**
 * HoleDetailSheet — slide-over panel showing full drill hole data
 * with an inline chat interface for asking questions about this hole.
 *
 * Opens from the right when a hole ID is clicked in the DrillHoleBrowser
 * or on the map. Contains:
 *   - Full collar metadata (type, depth, status, coordinates, dates)
 *   - Lithology summary
 *   - Inline chat scoped to this hole (persisted in localStorage)
 *
 * Props:
 *   holeId      {string|null}  - Hole to display (null = closed)
 *   projectId   {string}       - Active project UUID
 *   onClose     {function}     - Callback to close the sheet
 *   onNavigate  {function}     - Optional callback(holeId) to navigate to another hole
 */

interface ChatMessage {
    role: 'user' | 'assistant';
    content: string;
    ts: number;
    confidence?: number;
}

interface LithologyLog {
    from_depth: number;
    to_depth: number;
    lithology_code: string;
    lithology_description?: string | null;
}

// Numeric collar fields arrive as strings from the Laravel API (PG numeric
// columns serialise as strings), hence the parseFloat() call sites below.
interface Collar {
    collar_id: string;
    hole_id: string;
    hole_type?: string | null;
    status?: string | null;
    total_depth?: string | null;
    elevation?: string | null;
    easting?: string | null;
    northing?: string | null;
    azimuth?: string | null;
    dip?: string | null;
    drill_date?: string | null;
    lithology_logs?: LithologyLog[];
    surveys?: unknown[];
    samples?: unknown[];
}

interface HoleDetailSheetProps {
    holeId: string | null;
    projectId: string;
    onClose: () => void;
    onNavigate?: (holeId: string) => void;
}

// Per-hole chat history stored in localStorage
function getHoleHistory(holeId: string): ChatMessage[] {
    try {
        const raw = localStorage.getItem(`georag_hole_chat_${holeId}`);
        return raw ? JSON.parse(raw) : [];
    } catch {
        return [];
    }
}

function saveHoleHistory(holeId: string, messages: ChatMessage[]): void {
    try {
        localStorage.setItem(
            `georag_hole_chat_${holeId}`,
            JSON.stringify(messages.slice(-30)),  // keep last 30 per hole
        );
    } catch { /* quota */ }
}

export default function HoleDetailSheet({ holeId, projectId, onClose, onNavigate }: HoleDetailSheetProps) {
    const [collar, setCollar] = useState<Collar | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // Inline chat state — scoped to this hole
    const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
    const [chatInput, setChatInput] = useState('');
    const [chatLoading, setChatLoading] = useState(false);
    const chatEndRef = useRef<HTMLDivElement>(null);

    // Load collar data
    useEffect(() => {
        if (!holeId || !projectId) {
            setCollar(null);
            return;
        }

        setLoading(true);
        setError(null);

        // Auth via Sanctum session cookie (same-origin). No bearer token from
        // localStorage — localStorage is an XSS-exfiltration target (types.ts:11-12).
        fetch(`/api/v1/projects/${projectId}/collars?per_page=500`, {
            credentials: 'same-origin',
            headers: {
                Accept: 'application/json',
            },
        })
            .then((res) => {
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                return res.json();
            })
            .then((body) => {
                const list = body.data ?? body;
                const match = list.find((c) => c.hole_id === holeId);
                if (!match) throw new Error(`${holeId} not found`);

                // Fetch full record with lithology
                return fetch(`/api/v1/projects/${projectId}/collars/${match.collar_id}`, {
                    credentials: 'same-origin',
                    headers: {
                        Accept: 'application/json',
                    },
                });
            })
            .then((res) => {
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                return res.json();
            })
            .then((body) => {
                setCollar(body.data ?? body);
            })
            .catch((err) => setError(err.message))
            .finally(() => setLoading(false));
    }, [holeId, projectId]);

    // Load chat history for this hole
    useEffect(() => {
        if (holeId) {
            setChatMessages(getHoleHistory(holeId));
        }
    }, [holeId]);

    // Auto-scroll chat
    useEffect(() => {
        chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [chatMessages]);

    // Send a chat message scoped to this hole
    const handleChatSubmit = useCallback(async (e?: FormEvent<HTMLFormElement>) => {
        e?.preventDefault();
        const query = chatInput.trim();
        if (!query || chatLoading) return;

        const userMsg: ChatMessage = { role: 'user', content: query, ts: Date.now() };
        const updated = [...chatMessages, userMsg];
        setChatMessages(updated);
        setChatInput('');
        setChatLoading(true);

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');

            const res = await fetch('/api/v1/queries', {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    Accept: 'application/json',
                    ...(csrfToken ? { 'X-CSRF-TOKEN': csrfToken } : {}),
                },
                body: JSON.stringify({ query, project_id: projectId }),
            });

            if (!res.ok) throw new Error(`HTTP ${res.status}`);

            const { query_id, channel } = await res.json();

            // Subscribe to Reverb for streaming response
            const echoChannel = window.Echo.channel(channel);
            let accText = '';

            echoChannel.listen('.QueryStreamEvent', (event) => {
                if (event.event === 'delta' && event.token) {
                    accText += event.token;
                    setChatMessages((prev) => {
                        const msgs = [...prev];
                        const last = msgs[msgs.length - 1];
                        if (last?.role === 'assistant') {
                            msgs[msgs.length - 1] = { ...last, content: accText };
                        } else {
                            msgs.push({ role: 'assistant', content: accText, ts: Date.now() });
                        }
                        return msgs;
                    });
                } else if (event.event === 'completed') {
                    if (event.text) accText = event.text;
                    setChatMessages((prev) => {
                        const msgs = [...prev];
                        const last = msgs[msgs.length - 1];
                        if (last?.role === 'assistant') {
                            msgs[msgs.length - 1] = {
                                ...last,
                                content: accText,
                                confidence: event.confidence,
                            };
                        } else {
                            msgs.push({
                                role: 'assistant',
                                content: accText,
                                confidence: event.confidence,
                                ts: Date.now(),
                            });
                        }
                        // Persist
                        if (holeId) saveHoleHistory(holeId, msgs);
                        return msgs;
                    });
                    echoChannel.stopListening('.QueryStreamEvent');
                    window.Echo.leave(channel);
                    setChatLoading(false);
                } else if (event.event === 'failed' || event.event === 'error') {
                    setChatMessages((prev) => [
                        ...prev,
                        { role: 'assistant', content: `Error: ${event.error || 'Query failed'}`, ts: Date.now() },
                    ]);
                    echoChannel.stopListening('.QueryStreamEvent');
                    window.Echo.leave(channel);
                    setChatLoading(false);
                }
            });

            // Phase 2 — dispatch the job now that the listener is attached.
            // See QueryController::start() for the idempotency contract.
            // Auth via Sanctum session cookie (same-origin), matching the
            // /api/v1/queries POST above and Foundry/Chat.tsx's /start call.
            // No bearer token — localStorage is an XSS-exfiltration target.
            const startRes = await fetch(`/api/v1/queries/${query_id}/start`, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    Accept: 'application/json',
                    ...(csrfToken ? { 'X-CSRF-TOKEN': csrfToken } : {}),
                },
            });
            if (!startRes.ok && startRes.status !== 409) {
                echoChannel.stopListening('.QueryStreamEvent');
                window.Echo.leave(channel);
                throw new Error(`Failed to start query (${startRes.status})`);
            }
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            setChatMessages((prev) => [
                ...prev,
                { role: 'assistant', content: `Error: ${msg}`, ts: Date.now() },
            ]);
            setChatLoading(false);
        }
    }, [chatInput, chatLoading, chatMessages, holeId, projectId]);

    if (!holeId) return null;

    const lithologyLogs = collar?.lithology_logs ?? [];
    const surveys = collar?.surveys ?? [];
    const samples = collar?.samples ?? [];

    return (
        <>
            {/* Backdrop */}
            <div
                className="fixed inset-0 bg-black/50 z-40"
                onClick={onClose}
            />

            {/* Sheet */}
            <div className="fixed right-0 top-0 bottom-0 w-[480px] max-w-[90vw] bg-gray-900 border-l border-gray-700 z-50 flex flex-col shadow-2xl animate-in slide-in-from-right">

                {/* Header */}
                <div className="flex items-center justify-between px-5 py-4 border-b border-gray-800 shrink-0">
                    <div>
                        <h2 className="text-lg font-bold text-gray-100 font-mono">{holeId}</h2>
                        {collar && (
                            <p className="text-xs text-gray-500 mt-0.5">
                                {collar.hole_type} · {collar.total_depth ? `${parseFloat(collar.total_depth).toFixed(0)} m TD` : '—'} · {collar.status}
                            </p>
                        )}
                    </div>
                    <div className="flex items-center gap-2">
                        {collar && (
                            <button
                                type="button"
                                onClick={() => { onClose(); onNavigate?.(holeId); }}
                                className="text-xs text-amber-400 hover:text-amber-300 border border-amber-800/50 rounded px-2 py-1 transition-colors"
                            >
                                Open Strip Log
                            </button>
                        )}
                        <button
                            type="button"
                            onClick={onClose}
                            className="text-gray-400 hover:text-gray-200 p-1.5 rounded-lg hover:bg-gray-800 transition-colors"
                            aria-label="Close panel"
                        >
                        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" className="w-5 h-5">
                            <path fillRule="evenodd" d="M5.47 5.47a.75.75 0 0 1 1.06 0L12 10.94l5.47-5.47a.75.75 0 1 1 1.06 1.06L13.06 12l5.47 5.47a.75.75 0 1 1-1.06 1.06L12 13.06l-5.47 5.47a.75.75 0 0 1-1.06-1.06L10.94 12 5.47 6.53a.75.75 0 0 1 0-1.06Z" clipRule="evenodd" />
                        </svg>
                    </button>
                    </div>
                </div>

                {/* Content — flex column so chat fills remaining space */}
                <div className="flex-1 flex flex-col overflow-y-auto min-h-0">

                    {/* Loading */}
                    {loading && (
                        <div className="flex items-center justify-center py-12">
                            <div className="w-5 h-5 rounded-full border-2 border-gray-600 border-t-amber-400 animate-spin" />
                        </div>
                    )}

                    {/* Error */}
                    {error && (
                        <div className="mx-5 mt-4 text-xs text-red-400 bg-red-950/40 border border-red-800/40 rounded px-3 py-2">
                            {error}
                        </div>
                    )}

                    {/* Collar metadata */}
                    {collar && !loading && (
                        <div className="px-5 py-4 space-y-4">
                            {/* Metadata grid */}
                            <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
                                <MetaRow label="Hole Type" value={collar.hole_type} />
                                <MetaRow label="Status" value={collar.status} />
                                <MetaRow label="Total Depth" value={collar.total_depth ? `${parseFloat(collar.total_depth).toFixed(1)} m` : '—'} />
                                <MetaRow label="Elevation" value={collar.elevation ? `${parseFloat(collar.elevation).toFixed(0)} m` : '—'} />
                                <MetaRow label="Easting" value={collar.easting ? parseFloat(collar.easting).toFixed(1) : '—'} />
                                <MetaRow label="Northing" value={collar.northing ? parseFloat(collar.northing).toFixed(1) : '—'} />
                                <MetaRow label="Azimuth" value={collar.azimuth != null ? `${parseFloat(collar.azimuth).toFixed(1)}°` : '—'} />
                                <MetaRow label="Dip" value={collar.dip != null ? `${parseFloat(collar.dip).toFixed(1)}°` : '—'} />
                                <MetaRow label="Drill Date" value={collar.drill_date ?? '—'} />
                                <MetaRow label="Collar ID" value={collar.collar_id?.slice(0, 8) + '…'} mono />
                            </div>

                            {/* Lithology summary */}
                            {lithologyLogs.length > 0 && (
                                <div>
                                    <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">
                                        Lithology ({lithologyLogs.length} intervals)
                                    </h3>
                                    <div className="space-y-1">
                                        {lithologyLogs
                                            .sort((a, b) => a.from_depth - b.from_depth)
                                            .map((log, i) => (
                                                <div key={i} className="flex items-center gap-2 text-xs">
                                                    <span className="text-gray-500 font-mono w-24 shrink-0">
                                                        {log.from_depth}–{log.to_depth} m
                                                    </span>
                                                    <span className="font-mono text-amber-400 w-8">{log.lithology_code}</span>
                                                    <span className="text-gray-400 truncate">{log.lithology_description || '—'}</span>
                                                </div>
                                            ))}
                                    </div>
                                </div>
                            )}

                            {/* Sample count */}
                            {samples.length > 0 && (
                                <div className="text-xs text-gray-500">
                                    {samples.length} assay sample{samples.length !== 1 ? 's' : ''} logged
                                </div>
                            )}

                            {/* Survey count */}
                            {surveys.length > 0 && (
                                <div className="text-xs text-gray-500">
                                    {surveys.length} survey station{surveys.length !== 1 ? 's' : ''} logged
                                </div>
                            )}
                        </div>
                    )}

                    {/* ── Inline chat history — fills remaining space ── */}
                    <div className="border-t border-gray-800 px-5 py-3 flex flex-col flex-1 min-h-0">
                        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3 shrink-0">
                            Ask about {holeId}
                        </h3>

                        {chatMessages.length === 0 && (
                            <p className="text-xs text-gray-600 mb-3 shrink-0">
                                Ask a question about this drill hole — e.g. "What lithology is at 200m?" or "Summarise the assay results."
                            </p>
                        )}

                        <div className="space-y-2 overflow-y-auto flex-1 min-h-0 mb-3">
                            {chatMessages.map((msg, i) => (
                                <div
                                    key={i}
                                    className={`text-xs px-3 py-2 rounded-lg ${
                                        msg.role === 'user'
                                            ? 'bg-blue-600/20 text-blue-200 ml-8'
                                            : 'bg-gray-800 text-gray-300 mr-4'
                                    }`}
                                >
                                    <span className="text-[10px] text-gray-500 block mb-0.5">
                                        {msg.role === 'user' ? 'You' : 'GeoRAG'}
                                    </span>
                                    {msg.content}
                                    {msg.confidence != null && (
                                        <span className="ml-2 text-[10px] text-gray-600">
                                            ({Math.round(msg.confidence * 100)}% confidence)
                                        </span>
                                    )}
                                </div>
                            ))}

                            {chatLoading && (
                                <div className="flex items-center gap-1.5 px-3 py-2">
                                    <span className="w-1.5 h-1.5 rounded-full bg-gray-500 animate-bounce" style={{ animationDelay: '0ms' }} />
                                    <span className="w-1.5 h-1.5 rounded-full bg-gray-500 animate-bounce" style={{ animationDelay: '150ms' }} />
                                    <span className="w-1.5 h-1.5 rounded-full bg-gray-500 animate-bounce" style={{ animationDelay: '300ms' }} />
                                </div>
                            )}

                            <div ref={chatEndRef} />
                        </div>
                    </div>
                </div>

                {/* Chat input — always visible at bottom */}
                <div className="shrink-0 border-t border-gray-800 px-5 py-3 bg-gray-900">
                    <form onSubmit={handleChatSubmit} className="flex gap-2">
                        <input
                            type="text"
                            value={chatInput}
                            onChange={(e) => setChatInput(e.target.value)}
                            placeholder={`Ask about ${holeId}…`}
                            disabled={chatLoading}
                            className="flex-1 bg-gray-800 text-gray-100 text-xs border border-gray-700 rounded-lg px-3 py-2 focus:outline-none focus:ring-1 focus:ring-amber-500 placeholder-gray-500 disabled:opacity-50"
                            aria-label={`Chat about ${holeId}`}
                        />
                        <button
                            type="submit"
                            disabled={chatLoading || !chatInput.trim()}
                            className="bg-amber-600 hover:bg-amber-500 disabled:bg-gray-700 disabled:text-gray-500 text-white text-xs font-medium rounded-lg px-3 py-2 transition-colors"
                        >
                            Ask
                        </button>
                    </form>

                    {/* Quick question chips */}
                    <div className="flex flex-wrap gap-1.5 mt-2">
                        {[
                            `Summarise lithology for ${holeId}`,
                            `What is the deepest interval in ${holeId}?`,
                            `Show assay grades for ${holeId}`,
                        ].map((q) => (
                            <button
                                key={q}
                                type="button"
                                onClick={() => { setChatInput(q); }}
                                className="text-[10px] text-gray-500 hover:text-amber-400 border border-gray-700 hover:border-amber-700 rounded-full px-2 py-0.5 transition-colors"
                            >
                                {q.length > 35 ? q.slice(0, 35) + '…' : q}
                            </button>
                        ))}
                    </div>
                </div>
            </div>
        </>
    );
}

interface MetaRowProps {
    label: string;
    value: ReactNode;
    mono?: boolean;
}

function MetaRow({ label, value, mono = false }: MetaRowProps) {
    return (
        <div>
            <dt className="text-gray-500">{label}</dt>
            <dd className={`text-gray-200 ${mono ? 'font-mono' : ''}`}>{value}</dd>
        </div>
    );
}
