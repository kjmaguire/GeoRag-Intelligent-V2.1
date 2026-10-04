/**
 * Foundry/Chat — the terminal paths of one streamed answer, driven through a
 * fake Echo channel (no Reverb): the Workspace copilot hand-off (FE-4),
 * thread controls locked mid-answer (CHAT-3/12), heartbeats that keep the
 * phase text (CHAT-6), an uncited answer flagged (CHAT-16), Stop cancelling
 * the job (CHAT-18), and the removed synthesis toggle (CHAT-11); plus the
 * typed refusal/failure panels, the per-frame delta flush, the stale-timer
 * guard, the follow-the-bottom scroll rule and Stop persisting its partial.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act, cleanup, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';

vi.mock('@/Layouts/AppLayout', () => ({ default: ({ children }: { children: ReactNode }) => children }));
vi.mock('@inertiajs/react', () => ({
    Head: () => null,
    Link: ({ children }: { children: ReactNode }) => children,
    router: { get: vi.fn(), reload: vi.fn() },
}));
vi.mock('@/Components/InlineViz', () => ({ default: () => null }));

import { router } from '@inertiajs/react';
import FoundryChat from '../Chat';

type Handler = (event: Record<string, unknown>) => void;

const project = {
    project_id: '11111111-1111-1111-1111-111111111111',
    project_name: 'Shirley Basin',
    slug: 'shirley-basin',
};
const QUERY_ID = '22222222-2222-2222-2222-222222222222';

let handler: Handler | null = null;
let fetchCalls: Array<{ url: string; init?: RequestInit }> = [];
// Subscribe-ACK callbacks, when a test holds the ACK back instead of firing it.
let pendingAcks: Array<() => void> = [];

function installEcho({ ack = true }: { ack?: boolean } = {}) {
    pendingAcks = [];
    const channel = {
        listen: vi.fn((_: string, cb: Handler) => {
            handler = cb;
            return channel;
        }),
        subscribed: vi.fn((cb: () => void) => {
            if (ack) cb();
            else pendingAcks.push(cb);
        }),
        error: vi.fn(),
        stopListening: vi.fn(),
    };
    (window as unknown as Record<string, unknown>).Echo = {
        private: vi.fn(() => channel),
        leave: vi.fn(),
        connector: { pusher: { connection: { state: 'connected', bind: vi.fn(), unbind: vi.fn() } } },
    };
}

function installFetch() {
    fetchCalls = [];
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
        fetchCalls.push({ url: String(url), init });
        if (String(url) === '/api/v1/queries') {
            return new Response(JSON.stringify({ query_id: QUERY_ID, channel: 'query.' + QUERY_ID }), { status: 202 });
        }
        return new Response('{}', { status: 202 });
    }) as unknown as typeof fetch;
}

function renderChat(projectProps: Partial<typeof project> & { commodity?: string | null } = {}) {
    return render(
        <FoundryChat
            project={{ ...project, ...projectProps }}
            threads={[{ id: 'thread-a', title: 'Earlier thread', updated: '2026-09-28T00:00:00Z' }]}
            active_thread_id={null}
            active_thread={null}
            messages={[]}
            empty={false}
        />,
    );
}

async function ask(question: string) {
    fireEvent.change(screen.getByLabelText('Ask a question'), { target: { value: question } });
    await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: /send/i }));
    });
    await waitFor(() => expect(handler).not.toBeNull());
}

beforeEach(() => {
    handler = null;
    installEcho();
    installFetch();
    window.history.replaceState(null, '', '/projects/shirley-basin/chat');
});

afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.mocked(router.reload).mockClear();
});

// requestAnimationFrame under test control: frames run only when a test says so.
let frames = new Map<number, FrameRequestCallback>();
let nextFrameId = 1;
function stubAnimationFrames() {
    frames = new Map();
    nextFrameId = 1;
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
        const id = nextFrameId++;
        frames.set(id, cb);
        return id;
    });
    vi.stubGlobal('cancelAnimationFrame', (id: number) => {
        frames.delete(id);
    });
}
function runFrames() {
    act(() => {
        const due = [...frames.values()];
        frames.clear();
        due.forEach((cb) => cb(0));
    });
}

describe('Foundry chat', () => {
    it('prefills the question the Workspace copilot handed over (FE-4)', () => {
        window.history.replaceState(null, '', '/projects/shirley-basin/chat?prompt=Summarise%20the%20ore%20zones');
        renderChat();
        expect(screen.getByLabelText('Ask a question')).toHaveValue('Summarise the ore zones');
    });

    it('no longer offers the synthesis toggle nothing read (CHAT-11)', () => {
        renderChat();
        expect(screen.queryByText(/LLM synthesis/i)).toBeNull();
    });

    it('locks thread controls mid-answer and releases them on the terminal frame (CHAT-3/12)', async () => {
        renderChat();
        await ask('How deep is PLS-22-08?');

        expect(screen.getByRole('button', { name: '+ New' })).toBeDisabled();
        expect(screen.getByRole('button', { name: /Earlier thread/ })).toBeDisabled();

        await act(async () => {
            handler!({
                event: 'completed',
                text: 'PLS-22-08 is 412 m deep [DATA-1].',
                citations: [{ citation_id: '[DATA-1]', source_chunk_id: 'c1', citation_type: 'DATA' }],
                confidence: 0.9,
                validation_state: 'clean',
            });
        });

        expect(screen.getByRole('button', { name: '+ New' })).toBeEnabled();
    });

    it('keeps the phase text through a heartbeat (CHAT-6)', async () => {
        renderChat();
        await ask('How deep is PLS-22-08?');

        await act(async () => {
            handler!({ event: 'status', message: 'Synthesizing answer…', event_id: 's1' });
            handler!({ event: 'status', message: 'Analyzing query…', heartbeat: true, event_id: 'h1' });
        });

        expect(screen.getByText(/Synthesizing answer…/)).toBeInTheDocument();
    });

    it('flags a completed answer that carries no citations (CHAT-16)', async () => {
        renderChat();
        await ask('How deep is PLS-22-08?');

        await act(async () => {
            handler!({
                event: 'completed',
                text: 'It is 412 m deep.',
                citations: [],
                confidence: 0.9,
                validation_state: 'clean',
            });
        });

        expect(screen.getByTestId('no-citations-warning')).toHaveTextContent(/treat it as unverified/);
        expect(screen.getByText('It is 412 m deep.')).toBeInTheDocument();
        expect(screen.getByText('unverified')).toBeInTheDocument();
    });

    it('Stop asks the server to cancel the job (CHAT-18)', async () => {
        renderChat();
        await ask('How deep is PLS-22-08?');

        await act(async () => {
            fireEvent.click(screen.getByRole('button', { name: /Stop/ }));
        });

        expect(
            fetchCalls.some((c) => c.url === `/api/v1/queries/${QUERY_ID}/cancel` && c.init?.method === 'POST'),
        ).toBe(true);
    });

    it('exposes the transcript as a polite live log and marks the send button busy mid-answer', async () => {
        renderChat();
        const log = screen.getByRole('log');
        expect(log).toHaveAttribute('aria-live', 'polite');
        expect(log).toHaveAttribute('aria-busy', 'false');

        await ask('How deep is PLS-22-08?');

        expect(log).toHaveAttribute('aria-busy', 'true');
        expect(screen.getByRole('button', { name: /send/i })).toBeDisabled();
        expect(screen.getByRole('button', { name: /send/i })).toHaveAttribute('aria-busy', 'true');
    });

    it('keeps internal plumbing out of the footer and the error copy', () => {
        renderChat();
        expect(screen.queryByText(/Reverb/)).toBeNull();
        expect(screen.queryByText(/api\/v1\/citations/)).toBeNull();
        expect(screen.getByText(/enter sends/i)).toBeInTheDocument();
    });

    it('offers neutral suggestions without a commodity and fills the commodity in when the project has one', () => {
        const { unmount } = renderChat({ commodity: null });
        expect(screen.getByText(/What is the deepest hole on this project\?/)).toBeInTheDocument();
        expect(screen.getByText(/Summarise the lithology of the main zone/)).toBeInTheDocument();
        expect(screen.getByText(/Which reports mention a resource estimate\?/)).toBeInTheDocument();
        expect(screen.queryByText(/uranium|roll-front|Wyoming/i)).toBeNull();
        unmount();

        renderChat({ commodity: 'Gold' });
        expect(screen.getByText(/Compare mean Gold grade across holes/)).toBeInTheDocument();
    });

    describe('terminal frames', () => {
        it('renders a typed failure panel for a failed frame and keeps the streamed text', async () => {
            stubAnimationFrames();
            renderChat();
            await ask('How deep is PLS-22-08?');

            await act(async () => {
                // Buffered, not yet flushed: the failure must not lose it.
                handler!({ event: 'delta', token: 'PLS-22-08 reaches ', token_seq: 0, event_id: 'd0' });
                handler!({
                    event: 'failed',
                    error: 'Your query took too long to process.',
                    code: 'TIMEOUT',
                    event_id: 'f1',
                });
            });

            const panel = screen.getByTestId('refusal-panel');
            expect(panel).toHaveAttribute('data-variant', 'failed');
            expect(panel).toHaveTextContent('Your query took too long to process.');
            expect(panel).toHaveTextContent('Timeout');
            expect(screen.getByText(/PLS-22-08 reaches/)).toBeInTheDocument();
            // The failed turn is offered a retry without any hover.
            expect(screen.getByRole('button', { name: /Retry the question/ })).toBeEnabled();
        });

        it('renders the refusal panel from a completed frame carrying refusal_payload', async () => {
            renderChat();
            await ask('What is the gold price?');

            await act(async () => {
                handler!({
                    event: 'completed',
                    text: '',
                    citations: [],
                    confidence: 0.1,
                    validation_state: 'clean',
                    refusal_payload: {
                        type: 'refusal',
                        reason_code: 'insufficient_evidence',
                        message: 'Nothing in this project answers that.',
                        guard_codes: ['LAYER1_EMPTY'],
                    },
                });
            });

            const panel = screen.getByTestId('refusal-panel');
            expect(panel).toHaveAttribute('data-variant', 'refusal');
            expect(panel).toHaveAttribute('data-guard-codes', 'LAYER1_EMPTY');
            expect(panel.textContent ?? '').not.toMatch(/LAYER1_EMPTY/);
            expect(screen.getByText(/Insufficient evidence to answer this question/)).toBeInTheDocument();
            expect(screen.getByText('Nothing in this project answers that.')).toBeInTheDocument();
            // A refusal is not an uncited answer.
            expect(screen.queryByTestId('no-citations-warning')).toBeNull();
        });

        it('does not call a non-evidence refusal "insufficient evidence"', async () => {
            renderChat();
            await ask('Show PLS-1');

            await act(async () => {
                handler!({
                    event: 'completed',
                    text: '',
                    citations: [],
                    refusal_payload: {
                        type: 'refusal',
                        reason_code: 'AMBIGUOUS_HOLE_ID',
                        message: 'PLS-1 matches three holes.',
                    },
                });
            });

            expect(screen.getByText('Answer withheld')).toBeInTheDocument();
            expect(screen.queryByText(/Insufficient evidence/i)).toBeNull();
            expect(screen.getByText('PLS-1 matches three holes.')).toBeInTheDocument();
        });
    });

    describe('delta flushing', () => {
        it('flushes once per animation frame, in token_seq order, dropping re-delivered frames', async () => {
            stubAnimationFrames();
            renderChat();
            await ask('How deep is PLS-22-08?');
            const requestedBefore = nextFrameId;

            await act(async () => {
                handler!({ event: 'delta', token: 'grade ', token_seq: 1, event_id: 'e2' });
                handler!({ event: 'delta', token: 'The ', token_seq: 0, event_id: 'e1' });
                handler!({ event: 'delta', token: 'The ', token_seq: 0, event_id: 'e1' }); // re-delivered
                handler!({ event: 'delta', token: 'is 0.21%', token_seq: 2, event_id: 'e3' });
            });

            // Nothing rendered yet, and three accepted tokens asked for ONE frame.
            expect(screen.queryByText(/The grade/)).toBeNull();
            expect(nextFrameId - requestedBefore).toBe(1);

            runFrames();

            expect(screen.getByText(/The grade is 0\.21%/)).toBeInTheDocument();
            expect(screen.queryByText(/The The/)).toBeNull();

            // A later token schedules a fresh frame and appends.
            await act(async () => {
                handler!({ event: 'delta', token: ' at 412 m', token_seq: 3, event_id: 'e4' });
            });
            runFrames();
            expect(screen.getByText(/The grade is 0\.21% at 412 m/)).toBeInTheDocument();
        });

        it('a completed frame wins over a frame still waiting to flush', async () => {
            stubAnimationFrames();
            renderChat();
            await ask('How deep is PLS-22-08?');

            await act(async () => {
                handler!({ event: 'delta', token: 'draft text', token_seq: 0, event_id: 'e1' });
                handler!({
                    event: 'completed',
                    text: 'Final checked text [DATA-1].',
                    citations: [{ citation_id: '[DATA-1]', source_chunk_id: 'c1', citation_type: 'DATA' }],
                    confidence: 0.9,
                    validation_state: 'clean',
                });
            });
            runFrames();

            expect(screen.getByText(/Final checked text/)).toBeInTheDocument();
            expect(screen.queryByText(/draft text/)).toBeNull();
        });
    });

    describe('stale timers', () => {
        it('a Stop before the subscribe ACK or the 5 s fallback means the query is never started', async () => {
            installEcho({ ack: false });
            const fiveSecond: Array<() => void> = [];
            const realSetTimeout = globalThis.setTimeout;
            const spy = vi.spyOn(globalThis, 'setTimeout').mockImplementation(((
                fn: () => void,
                ms?: number,
                ...rest: unknown[]
            ) => {
                if (ms === 5_000) {
                    fiveSecond.push(fn);
                    return 0 as unknown as ReturnType<typeof setTimeout>;
                }
                return realSetTimeout(fn, ms, ...rest);
            }) as unknown as typeof setTimeout);
            try {
                renderChat();
                await ask('How deep is PLS-22-08?');
                expect(fiveSecond).toHaveLength(1);

                await act(async () => {
                    fireEvent.click(screen.getByRole('button', { name: /Stop/ }));
                });

                await act(async () => {
                    fiveSecond.forEach((fn) => fn());
                    pendingAcks.forEach((cb) => cb());
                });

                expect(fetchCalls.some((c) => c.url === `/api/v1/queries/${QUERY_ID}/start`)).toBe(false);
            } finally {
                spy.mockRestore();
            }
        });

        it('still starts the query when the 5 s fallback fires for the live stream', async () => {
            installEcho({ ack: false });
            const fiveSecond: Array<() => void> = [];
            const realSetTimeout = globalThis.setTimeout;
            const spy = vi.spyOn(globalThis, 'setTimeout').mockImplementation(((
                fn: () => void,
                ms?: number,
                ...rest: unknown[]
            ) => {
                if (ms === 5_000) {
                    fiveSecond.push(fn);
                    return 0 as unknown as ReturnType<typeof setTimeout>;
                }
                return realSetTimeout(fn, ms, ...rest);
            }) as unknown as typeof setTimeout);
            try {
                renderChat();
                await ask('How deep is PLS-22-08?');
                await act(async () => {
                    fiveSecond.forEach((fn) => fn());
                });
                expect(
                    fetchCalls.some((c) => c.url === `/api/v1/queries/${QUERY_ID}/start` && c.init?.method === 'POST'),
                ).toBe(true);
            } finally {
                spy.mockRestore();
            }
        });
    });

    describe('transcript scrolling', () => {
        function instrument(log: HTMLElement) {
            let top = 0;
            Object.defineProperty(log, 'scrollHeight', { configurable: true, get: () => 2000 });
            Object.defineProperty(log, 'clientHeight', { configurable: true, get: () => 400 });
            Object.defineProperty(log, 'scrollTop', {
                configurable: true,
                get: () => top,
                set: (v: number) => {
                    top = v;
                },
            });
            return {
                get top() {
                    return top;
                },
                scrollTo(v: number) {
                    top = v;
                    fireEvent.scroll(log);
                },
            };
        }

        it('follows new text while at the bottom and leaves a reader who scrolled up alone', async () => {
            stubAnimationFrames();
            renderChat();
            const scroller = instrument(screen.getByRole('log'));
            await ask('How deep is PLS-22-08?');
            // Sending follows the answer down.
            expect(scroller.top).toBe(2000);

            // The reader scrolls up to re-read something.
            scroller.scrollTo(300);
            await act(async () => {
                handler!({ event: 'delta', token: 'First part. ', token_seq: 0, event_id: 'e1' });
            });
            runFrames();
            expect(screen.getByText(/First part\./)).toBeInTheDocument();
            expect(scroller.top).toBe(300);

            // Back within 80 px of the bottom: following resumes.
            scroller.scrollTo(1600);
            await act(async () => {
                handler!({ event: 'delta', token: 'Second part.', token_seq: 1, event_id: 'e2' });
            });
            runFrames();
            expect(scroller.top).toBe(2000);
        });
    });

    describe('Stop', () => {
        it('marks the partial answer as stopped and persists it so it survives a reload', async () => {
            stubAnimationFrames();
            renderChat();
            await ask('How deep is PLS-22-08?');

            await act(async () => {
                handler!({ event: 'delta', token: 'PLS-22-08 is about ', token_seq: 0, event_id: 'e1' });
            });
            await act(async () => {
                fireEvent.click(screen.getByRole('button', { name: /Stop/ }));
            });

            expect(screen.getByText('Stopped by you. The text above is incomplete and unchecked.')).toBeInTheDocument();
            expect(screen.getByText(/PLS-22-08 is about/)).toBeInTheDocument();
            expect(screen.getByRole('button', { name: /Retry the question/ })).toBeEnabled();

            await waitFor(() =>
                expect(
                    fetchCalls.some((c) => c.url.startsWith('/api/v1/conversations/') && c.init?.method === 'PUT'),
                ).toBe(true),
            );
            const put = fetchCalls.find((c) => c.url.startsWith('/api/v1/conversations/') && c.init?.method === 'PUT')!;
            const body = JSON.parse(String(put.init?.body)) as {
                messages: Array<{ role: string; content: string; metadata: Record<string, unknown> }>;
            };
            const assistant = body.messages.find((m) => m.role === 'assistant')!;
            expect(assistant.content).toBe('PLS-22-08 is about ');
            expect(assistant.metadata.error).toBe('Stopped by you. The text above is incomplete and unchecked.');
        });
    });

    describe('lifecycle', () => {
        /** Wrap the installed fetch so one URL is held until the test releases it. */
        function holdFetch(
            match: (url: string) => boolean,
            respond?: () => Response,
        ): { release: () => void; held: () => boolean } {
            const inner = globalThis.fetch;
            let release: () => void = () => {};
            let hit = false;
            globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
                if (!hit && match(String(url))) {
                    hit = true;
                    await new Promise<void>((resolve) => {
                        release = resolve;
                    });
                    if (respond) return respond();
                }
                return inner(url, init);
            }) as unknown as typeof fetch;
            return { release: () => release(), held: () => hit };
        }

        it('does not subscribe to or start a run when the page unmounts while POST /queries is in flight', async () => {
            const gate = holdFetch((u) => u === '/api/v1/queries');
            const view = renderChat();
            fireEvent.change(screen.getByLabelText('Ask a question'), { target: { value: 'How deep is PLS-22-08?' } });
            await act(async () => {
                fireEvent.click(screen.getByRole('button', { name: /send/i }));
            });
            await waitFor(() => expect(gate.held()).toBe(true));

            view.unmount();
            await act(async () => {
                gate.release();
            });

            const echo = (window as unknown as { Echo: { private: ReturnType<typeof vi.fn> } }).Echo;
            expect(echo.private).not.toHaveBeenCalled();
            expect(fetchCalls.some((c) => c.url.endsWith('/start'))).toBe(false);
        });

        it('cancels a known run on the server when the page unmounts mid-stream', async () => {
            const view = renderChat();
            await ask('How deep is PLS-22-08?');

            view.unmount();

            expect(
                fetchCalls.some((c) => c.url === `/api/v1/queries/${QUERY_ID}/cancel` && c.init?.method === 'POST'),
            ).toBe(true);
        });

        it('does not reload the thread rail when a late PUT resolves after unmount', async () => {
            stubAnimationFrames();
            const gate = holdFetch((u) => u.startsWith('/api/v1/conversations/'));
            const view = renderChat();
            await ask('How deep is PLS-22-08?');

            await act(async () => {
                handler!({
                    event: 'completed',
                    text: 'It is 412 m deep [DATA-1].',
                    citations: [{ citation_id: '[DATA-1]', source_chunk_id: 'c1', citation_type: 'DATA' }],
                    confidence: 0.9,
                    validation_state: 'clean',
                });
            });
            await waitFor(() => expect(gate.held()).toBe(true));

            view.unmount();
            await act(async () => {
                gate.release();
            });

            expect(router.reload).not.toHaveBeenCalled();
        });

        it('a late recovery from a stopped query does not end the next stream', async () => {
            stubAnimationFrames();
            const gate = holdFetch(
                (u) => u === `/api/v1/queries/${QUERY_ID}/result`,
                () => new Response(JSON.stringify({ status: 'completed', text: 'A late answer' }), { status: 200 }),
            );
            renderChat();
            await ask('First question?');

            // A's terminal frame is flagged recoverable: the page asks the server for it.
            await act(async () => {
                handler!({ event: 'failed', recoverable: true, error: 'frame too large', event_id: 'f1' });
            });
            await waitFor(() => expect(gate.held()).toBe(true));

            await act(async () => {
                fireEvent.click(screen.getByRole('button', { name: /Stop/ }));
            });

            handler = null;
            await ask('Second question?');

            await act(async () => {
                gate.release();
            });
            // The server's answer for A arrives now, as a completed result.
            await act(async () => {});

            await act(async () => {
                handler!({ event: 'delta', token: 'B is still streaming', token_seq: 0, event_id: 'b1' });
            });
            runFrames();

            expect(screen.getByRole('button', { name: /Stop/ })).toBeInTheDocument();
            expect(screen.getByText(/B is still streaming/)).toBeInTheDocument();
            expect(screen.queryByText(/A late answer/)).toBeNull();
        });
    });

    describe('fail-closed access check frames', () => {
        it.each([
            [
                'ACCESS_CHECK_FAILED',
                'Could not check your access',
                'We could not verify your access to this project right now. Please try again in a few seconds.',
            ],
            [
                'SERVICE_UNAVAILABLE',
                'Service busy, try again',
                'The project could not be checked right now. Please try again in a few seconds.',
            ],
        ])('%s gets its own headline and offers Retry', async (code, headline, message) => {
            renderChat();
            await ask('How deep is PLS-22-08?');

            await act(async () => {
                handler!({ event: 'failed', code, error: message, event_id: 'f1' });
            });

            expect(screen.getByText(headline)).toBeInTheDocument();
            expect(screen.getByText(message)).toBeInTheDocument();
            expect(screen.getByRole('button', { name: /Retry the question/ })).toBeEnabled();
        });
    });

    describe('thread rail', () => {
        it('refreshes threads after a successful sync without reloading the transcript props', async () => {
            renderChat();
            await ask('How deep is PLS-22-08?');

            await act(async () => {
                handler!({
                    event: 'completed',
                    text: 'It is 412 m deep [DATA-1].',
                    citations: [{ citation_id: '[DATA-1]', source_chunk_id: 'c1', citation_type: 'DATA' }],
                    confidence: 0.9,
                    validation_state: 'clean',
                });
            });

            await waitFor(() => expect(router.reload).toHaveBeenCalledWith({ only: ['threads', 'active_thread'] }));
            // The answer is still on screen.
            expect(screen.getByText(/It is 412 m deep/)).toBeInTheDocument();
        });
    });

    describe('citation chips', () => {
        it('titles a chip with its document, never the raw chunk id, and highlights only the chip that was opened', async () => {
            renderChat();
            await ask('How deep is PLS-22-08?');
            await act(async () => {
                handler!({
                    event: 'completed',
                    text: 'Two sources.',
                    citations: [
                        {
                            citation_id: '',
                            source_chunk_id: 'georag_reports:r1:section=7:chunk=aaa',
                            citation_type: 'NI43',
                            document_title: 'Report One',
                        },
                        {
                            citation_id: '',
                            source_chunk_id: 'georag_reports:r2:section=3:chunk=bbb',
                            citation_type: 'NI43',
                            document_title: 'Report Two',
                        },
                    ],
                    confidence: 0.9,
                    validation_state: 'clean',
                });
            });

            const chips = screen.getAllByRole('button', { name: /Report (One|Two)/ });
            expect(chips).toHaveLength(2);
            for (const chip of chips) {
                expect(chip.getAttribute('title') ?? '').not.toMatch(/georag_reports|chunk=/);
            }
            expect(chips[0]).toHaveAttribute('title', '[1] Report One');
            expect(chips[0].style.borderColor).toBe('var(--line-2)');
            expect(chips[1].style.borderColor).toBe('var(--line-2)');
        });
    });
});
