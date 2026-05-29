import type { JSX } from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Head } from '@inertiajs/react';
import AppLayout from '../Layouts/AppLayout';
import { EvidenceInspector } from '../Components/chat/EvidenceInspector';
import type { Citation } from '@/types';

/**
 * /search — R-P11-B Search/Query frontend.
 *
 * Slice 1 (Phase 39) shipped the skeleton. Slice 2 (Phase 40) wired
 * the SSE handshake. Slice 3 (Phase 41) added the EvidenceInspector
 * citation surface. Slice 4 (Phase 42) adds a localStorage-backed
 * history panel (last 10 queries) plus URL deep-link via ?q=… —
 * the page now bookmarks: paste /search?q=…, get the answer.
 *
 * Slice 5 wires the top-nav entry.
 */
const SEARCH_HISTORY_KEY = 'georag.search.history.v1';
const SEARCH_HISTORY_MAX = 10;

// `window.Echo` declared globally in bootstrap.ts as `any`. Local shape:
interface EchoChannel {
    listen: (event: string, callback: (e: Record<string, unknown>) => void) => EchoChannel;
    stopListening: (event: string) => void;
}

interface SearchResult {
    answer: string;
    citations: Citation[];
    confidence: number | null;
}

interface InspectorState {
    open: boolean;
    evidenceId: string | null;
    legacyCitation: Citation | null;
}

interface HistoryEntry {
    query: string;
    asked_at: string;
}

function loadHistory(): HistoryEntry[] {
    try {
        const raw = window.localStorage.getItem(SEARCH_HISTORY_KEY);
        if (!raw) return [];
        const parsed = JSON.parse(raw) as unknown;
        if (!Array.isArray(parsed)) return [];
        return (parsed as HistoryEntry[])
            .filter((e) => typeof e?.query === 'string' && typeof e?.asked_at === 'string')
            .slice(0, SEARCH_HISTORY_MAX);
    } catch {
        return [];
    }
}

function saveHistory(entries: HistoryEntry[]): void {
    try {
        window.localStorage.setItem(SEARCH_HISTORY_KEY, JSON.stringify(entries.slice(0, SEARCH_HISTORY_MAX)));
    } catch {
        // localStorage may be disabled (private mode); history degrades silently.
    }
}

function pushHistory(query: string): HistoryEntry[] {
    const trimmed = query.trim();
    if (trimmed.length === 0) return loadHistory();
    const existing = loadHistory().filter((e) => e.query !== trimmed);
    const next = [{ query: trimmed, asked_at: new Date().toISOString() }, ...existing].slice(
        0,
        SEARCH_HISTORY_MAX,
    );
    saveHistory(next);
    return next;
}

const CITATION_KIND_ICON: Record<Citation['citation_type'], string> = {
    NI43: '📄',
    PUB: '📚',
    DATA: '📊',
    PGEO: '🗺️',
};

export default function SearchQuery(): JSX.Element {
    const [query, setQuery] = useState<string>('');
    const [phase, setPhase] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [result, setResult] = useState<SearchResult | null>(null);
    const [inspector, setInspector] = useState<InspectorState>({
        open: false,
        evidenceId: null,
        legacyCitation: null,
    });
    const [history, setHistory] = useState<HistoryEntry[]>([]);
    const channelRef = useRef<{ ch: EchoChannel; name: string } | null>(null);
    const autoSubmitRef = useRef<boolean>(false);

    const csrf = (): string | null =>
        document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') ?? null;

    const cleanup = (): void => {
        if (channelRef.current) {
            channelRef.current.ch.stopListening('.QueryStreamEvent');
            window.Echo.leave(channelRef.current.name);
            channelRef.current = null;
        }
    };

    const runQuery = useCallback(async (text: string): Promise<void> => {
        const trimmed = text.trim();
        if (trimmed.length === 0) return;
        cleanup();
        setError(null);
        setResult(null);
        setPhase('Submitting query…');
        setHistory(pushHistory(trimmed));

        // URL deep-link: reflect the active query in the address bar so the
        // current view is bookmarkable. replaceState (not pushState) — the
        // form is the same page; no spurious back-button entries.
        try {
            const url = new URL(window.location.href);
            url.searchParams.set('q', trimmed);
            window.history.replaceState({}, '', url.toString());
        } catch {
            // URL API failure (very old browsers) is non-fatal.
        }

        const token = csrf();
        try {
            const createResp = await fetch('/api/v1/queries', {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    Accept: 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                    ...(token ? { 'X-CSRF-TOKEN': token } : {}),
                },
                body: JSON.stringify({ query: trimmed }),
            });
            if (!createResp.ok) {
                throw new Error(`create failed (${createResp.status})`);
            }
            const { query_id, channel } = (await createResp.json()) as {
                query_id: string;
                channel: string;
            };

            const ch = window.Echo.channel(channel);
            channelRef.current = { ch, name: channel };
            setPhase('Waiting for response…');

            ch.listen('.QueryStreamEvent', (event: Record<string, unknown>) => {
                const eventType = event.event as string | undefined;
                if (eventType === 'status' && typeof event.message === 'string') {
                    setPhase(event.message);
                } else if (eventType === 'completed') {
                    setResult({
                        answer: (event.answer as string) ?? '',
                        citations: (event.citations as Citation[]) ?? [],
                        confidence: (event.confidence as number | null) ?? null,
                    });
                    setPhase(null);
                    cleanup();
                } else if (eventType === 'failed' || eventType === 'error') {
                    setError((event.error as string) ?? (event.message as string) ?? 'Query failed');
                    setPhase(null);
                    cleanup();
                }
            });

            const startResp = await fetch(`/api/v1/queries/${query_id}/start`, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    Accept: 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                    ...(token ? { 'X-CSRF-TOKEN': token } : {}),
                },
            });
            if (!startResp.ok && startResp.status !== 409) {
                throw new Error(`start failed (${startResp.status})`);
            }
        } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
            setPhase(null);
            cleanup();
        }
    }, []);

    const handleSubmit = (e: React.FormEvent): void => {
        e.preventDefault();
        void runQuery(query);
    };

    // Mount: hydrate history from localStorage. If ?q=… is present, also
    // populate the input and auto-submit so /search?q=foo is bookmarkable.
    useEffect(() => {
        setHistory(loadHistory());
        try {
            const params = new URLSearchParams(window.location.search);
            const q = params.get('q');
            if (q && q.trim().length > 0 && !autoSubmitRef.current) {
                autoSubmitRef.current = true;
                setQuery(q);
                void runQuery(q);
            }
        } catch {
            // URLSearchParams unsupported — skip auto-submit.
        }
    }, [runQuery]);

    const handleRerun = (entry: HistoryEntry): void => {
        setQuery(entry.query);
        void runQuery(entry.query);
    };

    const handleClearHistory = (): void => {
        saveHistory([]);
        setHistory([]);
    };

    const busy = phase !== null;

    return (
        <AppLayout>
            <Head title="Search" />
            <div className="max-w-3xl mx-auto p-6 space-y-6">
                <header>
                    <h1 className="text-2xl font-semibold text-gray-900">Search</h1>
                    <p className="text-sm text-gray-600 mt-1">
                        Ask one question, get one cited answer. For multi-turn
                        exploration use <a href="/chat" className="text-blue-700 underline">chat</a>.
                    </p>
                </header>

                <form className="flex items-center gap-2" onSubmit={handleSubmit}>
                    <input
                        type="text"
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                        placeholder="e.g. What is the average gold grade at Triple R?"
                        className="flex-1 border rounded px-3 py-2 text-sm"
                        aria-label="Search query"
                        disabled={busy}
                    />
                    <button
                        type="submit"
                        disabled={busy || query.trim().length === 0}
                        className="border rounded px-4 py-2 text-sm bg-blue-600 text-white disabled:bg-gray-300"
                    >
                        Ask
                    </button>
                </form>

                {phase && (
                    <div className="text-sm text-gray-500 italic" aria-live="polite">
                        {phase}
                    </div>
                )}

                {error && (
                    <div className="border border-red-200 bg-red-50 text-red-800 rounded p-3 text-sm">
                        Failed: <code className="font-mono">{error}</code>
                    </div>
                )}

                {result && !error && (
                    <section
                        aria-label="Search results"
                        className="border rounded p-4 bg-white space-y-3"
                    >
                        <div className="text-sm whitespace-pre-wrap text-gray-900">
                            {result.answer || '(empty answer)'}
                        </div>
                        {result.confidence !== null && (
                            <div className="text-xs text-gray-500">
                                Confidence: {(result.confidence * 100).toFixed(1)}%
                            </div>
                        )}
                        {result.citations.length > 0 && (
                            <div className="pt-3 border-t space-y-1">
                                <h3 className="text-xs uppercase tracking-wide text-gray-500 mb-2">
                                    {result.citations.length} citation
                                    {result.citations.length === 1 ? '' : 's'}
                                </h3>
                                <ul className="space-y-1">
                                    {result.citations.map((c) => (
                                        <li key={c.citation_id}>
                                            <button
                                                type="button"
                                                onClick={() =>
                                                    setInspector({
                                                        open: true,
                                                        evidenceId: c.citation_id,
                                                        legacyCitation: c,
                                                    })
                                                }
                                                className="w-full text-left text-xs flex items-start gap-2 hover:bg-gray-50 rounded px-2 py-1.5 border border-transparent hover:border-gray-200"
                                            >
                                                <span className="select-none" aria-hidden="true">
                                                    {CITATION_KIND_ICON[c.citation_type] ?? '📎'}
                                                </span>
                                                <span className="flex-1 min-w-0">
                                                    <span className="block truncate font-medium text-gray-900">
                                                        {c.document_title || c.source_chunk_id}
                                                    </span>
                                                    <span className="block text-gray-500 truncate">
                                                        {c.section_title ?? c.section ?? c.section_number ?? '—'}
                                                        {typeof c.page === 'number' ? ` · p.${c.page}` : ''}
                                                        {' · '}
                                                        {(c.relevance_score * 100).toFixed(0)}%
                                                    </span>
                                                </span>
                                                <span className="font-mono text-[10px] text-gray-400 shrink-0">
                                                    {c.citation_type}
                                                </span>
                                            </button>
                                        </li>
                                    ))}
                                </ul>
                            </div>
                        )}
                    </section>
                )}

                {!busy && !result && !error && (
                    <section
                        aria-label="Search results"
                        className="border rounded p-6 bg-gray-50 text-sm text-gray-500 min-h-[160px] flex items-center justify-center"
                    >
                        Results will appear here once you submit a query.
                    </section>
                )}

                {history.length > 0 && (
                    <section aria-label="Recent queries" className="pt-4 border-t">
                        <div className="flex items-center justify-between mb-2">
                            <h2 className="text-xs uppercase tracking-wide text-gray-500">
                                Recent queries
                            </h2>
                            <button
                                type="button"
                                onClick={handleClearHistory}
                                className="text-xs text-gray-400 hover:text-gray-600 underline"
                            >
                                Clear
                            </button>
                        </div>
                        <ul className="space-y-1">
                            {history.map((entry) => (
                                <li key={entry.asked_at}>
                                    <button
                                        type="button"
                                        onClick={() => handleRerun(entry)}
                                        disabled={busy}
                                        className="w-full text-left text-xs flex items-baseline gap-2 hover:bg-gray-50 rounded px-2 py-1 disabled:opacity-50"
                                    >
                                        <span className="text-gray-400 select-none">↻</span>
                                        <span className="flex-1 truncate text-gray-800">
                                            {entry.query}
                                        </span>
                                        <span className="text-[10px] text-gray-400 shrink-0">
                                            {new Date(entry.asked_at).toLocaleTimeString()}
                                        </span>
                                    </button>
                                </li>
                            ))}
                        </ul>
                    </section>
                )}

                <footer className="text-xs text-gray-400 pt-4 border-t">
                    R-P11-B complete — Phase 43. Slices 1–5: skeleton, SSE
                    wiring, citation rendering, history + deep-link, top-nav.
                </footer>
            </div>
            <EvidenceInspector
                open={inspector.open}
                onOpenChange={(open) => {
                    if (!open) {
                        setInspector({ open: false, evidenceId: null, legacyCitation: null });
                    }
                }}
                evidenceId={inspector.evidenceId}
                legacyCitation={inspector.legacyCitation}
            />
        </AppLayout>
    );
}
