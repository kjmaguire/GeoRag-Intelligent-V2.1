/**
 * Chat.test.tsx
 *
 * Security regression guard: Chat page must NOT read auth tokens from
 * localStorage. The four fetch call sites (_authedFetch, runQueryHandshake,
 * replayMissedEvents, SourceViewer.loadSource) all use Sanctum session cookie
 * via `credentials: 'same-origin'` (types.ts:11-12).
 *
 * Note: Chat.tsx legitimately reads non-auth keys from localStorage:
 *   - `georag_chat_threads` (thread index)
 *   - `georag_chat_thread_<id>` (per-thread messages)
 *   - `georag_chat_messages` (legacy migration source)
 * None of these match /token|jwt|secret/i.
 *
 * Approach: render the page with mocked Inertia + Echo; assert that on mount
 * no token-key localStorage reads occur. Full streaming paths are not triggered
 * here (they require WebSocket + query submission) — the on-mount regression
 * guard is sufficient for this security test category.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render } from '@testing-library/react';

// jsdom does not implement scrollIntoView — stub it globally so Chat.tsx's
// messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' }) doesn't throw.
if (typeof Element.prototype.scrollIntoView === 'undefined') {
    Element.prototype.scrollIntoView = vi.fn();
}

// Mock Inertia
vi.mock('@inertiajs/react', () => ({
    Head: ({ children }: any) => <>{children}</>,
    usePage: vi.fn(() => ({
        props: {
            auth: { user: { name: 'Kyle', email: 'k@example.com' } },
            workspace: { data_version: 1 },
        },
        url: '/chat',
    })),
    router: { visit: vi.fn() },
}));

// Mock heavy child components that pull in Plotly or MapLibre
vi.mock('../../Components/MapView', () => ({ default: () => <div data-testid="map-stub" /> }));
vi.mock('../../Components/ChatMessage', () => ({ default: () => <div data-testid="msg-stub" /> }));
vi.mock('../../Components/ProjectContextBanner', () => ({
    default: () => <div data-testid="banner-stub" />,
}));
vi.mock('../../Components/PublicGeoscience/CitationPGEODetail', () => ({
    default: () => <div data-testid="pgeo-stub" />,
}));
vi.mock('../../Components/chat/EvidenceInspector', () => ({
    EvidenceInspector: () => <div data-testid="evidence-stub" />,
}));
vi.mock('../../Layouts/AppLayout', () => ({
    default: ({ children }: any) => <div>{children}</div>,
}));
vi.mock('../../Components/ProjectSelector', () => ({
    default: () => <div data-testid="selector-stub" />,
}));

// Stub Laravel Echo — Chat.tsx accesses window.Echo in effect hooks triggered
// by streaming events, not on mount, but stub it to avoid reference errors.
(globalThis as any).window = (globalThis as any).window ?? {};
(globalThis as any).window.Echo = {
    channel: vi.fn(() => ({ listen: vi.fn().mockReturnThis(), stopListening: vi.fn() })),
    leave: vi.fn(),
};

import Chat from '../Chat';

describe('Chat page — auth surface', () => {
    let getItemSpy: ReturnType<typeof vi.spyOn>;

    beforeEach(() => {
        getItemSpy = vi.spyOn(Storage.prototype, 'getItem');
        vi.spyOn(globalThis, 'fetch').mockResolvedValue(
            new Response(JSON.stringify({}), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            }),
        );
    });

    afterEach(() => {
        getItemSpy.mockRestore();
        vi.restoreAllMocks();
    });

    it('does not read auth tokens from localStorage on mount', () => {
        render(<Chat />);

        const tokenLike = /token|jwt|secret/i;
        const offending = getItemSpy.mock.calls
            .map(([key]) => String(key))
            .filter((k) => tokenLike.test(k));
        expect(offending).toEqual([]);
    });
});
