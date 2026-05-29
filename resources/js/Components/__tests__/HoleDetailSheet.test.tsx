/**
 * HoleDetailSheet.test.tsx
 *
 * Security regression guard: HoleDetailSheet must NOT read auth tokens from
 * localStorage. Its two collar fetch calls use Sanctum session cookie via
 * `credentials: 'same-origin'` (types.ts:11-12).
 *
 * Note: HoleDetailSheet legitimately reads `georag_hole_chat_<holeId>` from
 * localStorage for per-hole chat history persistence. That key does NOT match
 * /token|jwt|secret/i and is intentionally kept.
 *
 * window.Echo is stubbed because HoleDetailSheet references it in the chat
 * submit path (not triggered here, but the global reference must exist to
 * avoid ReferenceError during render).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import HoleDetailSheet from '../HoleDetailSheet';

// jsdom does not implement scrollIntoView — stub it globally so
// chatEndRef.current?.scrollIntoView({ behavior: 'smooth' }) doesn't throw.
if (typeof Element.prototype.scrollIntoView === 'undefined') {
    Element.prototype.scrollIntoView = vi.fn();
}

// Stub Laravel Echo to prevent ReferenceError
(globalThis as any).window = (globalThis as any).window ?? {};
(globalThis as any).window.Echo = {
    channel: vi.fn(() => ({ listen: vi.fn().mockReturnThis(), stopListening: vi.fn() })),
    leave: vi.fn(),
};

describe('HoleDetailSheet — auth surface', () => {
    let getItemSpy: ReturnType<typeof vi.spyOn>;
    let fetchSpy: ReturnType<typeof vi.spyOn>;

    const collarList = [{ collar_id: 'col-1', hole_id: 'DH-001' }];
    const collarDetail = {
        collar_id: 'col-1',
        hole_id: 'DH-001',
        project_id: 'proj-abc',
        total_depth: 250,
        lithology_logs: [],
    };

    beforeEach(() => {
        getItemSpy = vi.spyOn(Storage.prototype, 'getItem');
        // First call returns collar list; second returns collar detail.
        fetchSpy = vi.spyOn(globalThis, 'fetch')
            .mockResolvedValueOnce(
                new Response(JSON.stringify({ data: collarList }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                }),
            )
            .mockResolvedValueOnce(
                new Response(JSON.stringify({ data: collarDetail }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                }),
            );
    });

    afterEach(() => {
        getItemSpy.mockRestore();
        fetchSpy.mockRestore();
    });

    it('does not read auth tokens from localStorage during collar fetch', async () => {
        render(
            <HoleDetailSheet
                holeId="DH-001"
                projectId="proj-abc"
                onClose={vi.fn()}
                onNavigate={vi.fn()}
            />,
        );
        await waitFor(() => expect(fetchSpy).toHaveBeenCalled());

        const tokenLike = /token|jwt|secret/i;
        const offending = getItemSpy.mock.calls
            .map(([key]: [string]) => key)
            .filter((k) => tokenLike.test(k));
        expect(offending).toEqual([]);
    });

    it('collar index fetch uses same-origin credentials', async () => {
        render(
            <HoleDetailSheet
                holeId="DH-001"
                projectId="proj-abc"
                onClose={vi.fn()}
                onNavigate={vi.fn()}
            />,
        );
        await waitFor(() => expect(fetchSpy).toHaveBeenCalled());

        const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
        expect(init?.credentials).toBe('same-origin');
        const headers = (init?.headers ?? {}) as Record<string, string>;
        expect(headers['Authorization']).toBeUndefined();
    });
});
