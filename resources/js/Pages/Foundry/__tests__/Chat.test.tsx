/**
 * Foundry/Chat — the terminal paths of one streamed answer, driven through a
 * fake Echo channel (no Reverb): the Workspace copilot hand-off (FE-4),
 * thread controls locked mid-answer (CHAT-3/12), heartbeats that keep the
 * phase text (CHAT-6), an uncited answer flagged (CHAT-16), Stop cancelling
 * the job (CHAT-18), and the removed synthesis toggle (CHAT-11).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act, cleanup, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';

vi.mock('@/Layouts/AppLayout', () => ({ default: ({ children }: { children: ReactNode }) => children }));
vi.mock('@inertiajs/react', () => ({
    Head: () => null,
    Link: ({ children }: { children: ReactNode }) => children,
    router: { get: vi.fn() },
}));
vi.mock('@/Components/InlineViz', () => ({ default: () => null }));

import FoundryChat from '../Chat';

type Handler = (event: Record<string, unknown>) => void;

const project = { project_id: '11111111-1111-1111-1111-111111111111', project_name: 'Shirley Basin', slug: 'shirley-basin' };
const QUERY_ID = '22222222-2222-2222-2222-222222222222';

let handler: Handler | null = null;
let fetchCalls: Array<{ url: string; init?: RequestInit }> = [];

function installEcho() {
    const channel = {
        listen: vi.fn((_: string, cb: Handler) => {
            handler = cb;
            return channel;
        }),
        subscribed: vi.fn((cb: () => void) => cb()),
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

function renderChat() {
    return render(
        <FoundryChat
            project={project}
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
});

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
            handler!({ event: 'completed', text: 'PLS-22-08 is 412 m deep [DATA-1].', citations: [{ citation_id: '[DATA-1]', source_chunk_id: 'c1', citation_type: 'DATA' }], confidence: 0.9, validation_state: 'clean' });
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
            handler!({ event: 'completed', text: 'It is 412 m deep.', citations: [], confidence: 0.9, validation_state: 'clean' });
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

        expect(fetchCalls.some((c) => c.url === `/api/v1/queries/${QUERY_ID}/cancel` && c.init?.method === 'POST')).toBe(true);
    });
});
