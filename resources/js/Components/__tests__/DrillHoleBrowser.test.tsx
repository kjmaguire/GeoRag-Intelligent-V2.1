/**
 * DrillHoleBrowser.test.tsx
 *
 * Security regression guard: DrillHoleBrowser must NOT read auth tokens from
 * localStorage. Its fetchCollars call uses Sanctum session cookie via
 * `credentials: 'same-origin'` (types.ts:11-12).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import DrillHoleBrowser from '../DrillHoleBrowser';

describe('DrillHoleBrowser — auth surface', () => {
    let getItemSpy: ReturnType<typeof vi.spyOn>;
    let fetchSpy: ReturnType<typeof vi.spyOn>;

    const collarList = [
        {
            collar_id: 'col-1',
            hole_id: 'DH-001',
            hole_type: 'Diamond',
            status: 'Completed',
            total_depth: 350,
            easting: 500000,
            northing: 6200000,
        },
    ];

    beforeEach(() => {
        getItemSpy = vi.spyOn(Storage.prototype, 'getItem');
        fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
            new Response(JSON.stringify({ data: collarList }), {
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
            <DrillHoleBrowser
                projectId="proj-abc"
                onHoleClick={vi.fn()}
                selectedHoleId={null}
            />,
        );
        await waitFor(() => expect(fetchSpy).toHaveBeenCalled());

        const tokenLike = /token|jwt|secret/i;
        const offending = getItemSpy.mock.calls
            .map(([key]: [string]) => key)
            .filter((k) => tokenLike.test(k));
        expect(offending).toEqual([]);
    });

    it('collar fetch uses same-origin credentials', async () => {
        render(
            <DrillHoleBrowser
                projectId="proj-abc"
                onHoleClick={vi.fn()}
                selectedHoleId={null}
            />,
        );
        await waitFor(() => expect(fetchSpy).toHaveBeenCalled());

        const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
        expect(init?.credentials).toBe('same-origin');
        const headers = (init?.headers ?? {}) as Record<string, string>;
        expect(headers['Authorization']).toBeUndefined();
    });
});
