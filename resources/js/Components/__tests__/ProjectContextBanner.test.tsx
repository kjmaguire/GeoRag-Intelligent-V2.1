/**
 * ProjectContextBanner.test.tsx
 *
 * Guards the security fix that removed `localStorage.getItem('georag_token')`
 * from this component. localStorage is untrusted (per types.ts:11-12) and an
 * XSS-exfiltration target; auth must ride the Sanctum session cookie via
 * `credentials: 'same-origin'`.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, waitFor } from '@testing-library/react';

import ProjectContextBanner from '../ProjectContextBanner';

describe('ProjectContextBanner — auth surface', () => {
    let getItemSpy: ReturnType<typeof vi.spyOn>;
    let fetchSpy: ReturnType<typeof vi.spyOn>;

    beforeEach(() => {
        getItemSpy = vi.spyOn(Storage.prototype, 'getItem');
        fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
            new Response(JSON.stringify({ data: null }), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            }),
        );
    });

    afterEach(() => {
        getItemSpy.mockRestore();
        fetchSpy.mockRestore();
    });

    it('does not read any token-like value from localStorage', async () => {
        render(<ProjectContextBanner projectId="abc-123" />);
        await waitFor(() => expect(fetchSpy).toHaveBeenCalled());

        const tokenLike = /token|jwt|secret/i;
        const offendingKeys = getItemSpy.mock.calls
            .map((call) => String(call[0]))
            .filter((key) => tokenLike.test(key));
        expect(offendingKeys).toEqual([]);
    });

    it('sends the request with same-origin credentials so the Sanctum cookie carries auth', async () => {
        render(<ProjectContextBanner projectId="abc-123" />);
        await waitFor(() => expect(fetchSpy).toHaveBeenCalled());

        const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
        expect(init?.credentials).toBe('same-origin');

        const headers = (init?.headers ?? {}) as Record<string, string>;
        expect(headers.Authorization).toBeUndefined();
    });
});
