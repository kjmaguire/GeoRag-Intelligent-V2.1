/**
 * StripLogViewer.test.tsx
 *
 * Security regression guard: StripLogViewer must NOT read auth tokens from
 * localStorage. Its two fetch calls (collar index + collar detail with
 * lithology/well_log_curves) use Sanctum session cookie via
 * `credentials: 'same-origin'` (types.ts:11-12).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor, act } from '@testing-library/react';
import StripLogViewer from '../StripLogViewer';

describe('StripLogViewer — auth surface', () => {
    let getItemSpy: ReturnType<typeof vi.spyOn>;
    let fetchSpy: ReturnType<typeof vi.spyOn>;

    const collarPayload = {
        collar_id: 'col-001',
        hole_id: 'DH-001',
        project_id: 'proj-abc',
        total_depth: 350,
        azimuth: 180,
        dip: -60,
        lithology_logs: [],
        well_log_curves: [],
    };

    beforeEach(() => {
        getItemSpy = vi.spyOn(Storage.prototype, 'getItem');
        fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
            new Response(JSON.stringify({ data: collarPayload }), {
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
        render(<StripLogViewer holeId="DH-001" projectId="proj-abc" />);
        await waitFor(() => expect(fetchSpy).toHaveBeenCalled());

        const tokenLike = /token|jwt|secret/i;
        const offending = getItemSpy.mock.calls.map(([key]) => String(key)).filter((k) => tokenLike.test(k));
        expect(offending).toEqual([]);
    });

    it('a collar with no total depth is scaled to its deepest interval, not to zero (§04e 2026-09-29)', async () => {
        const noTd = {
            ...collarPayload,
            total_depth: null,
            lithology_logs: [{ log_id: 'l1', from_depth: 0, to_depth: 120, lithology_code: 'SST' }],
        };
        // First the collar index (a list), then the collar itself.
        fetchSpy.mockImplementation(
            async (url: RequestInfo | URL) =>
                new Response(JSON.stringify({ data: String(url).includes('/collars?') ? [noTd] : noTd }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                }),
        );

        const { container } = render(<StripLogViewer holeId="DH-001" projectId="proj-abc" />);
        await waitFor(() => expect(container.textContent).toContain('SST'));

        // The depth axis reaches the 100 m tick (and stops before 150): the
        // scale came from the 120 m interval, and no "m TD" is claimed.
        expect(container.textContent).toContain('100');
        expect(container.textContent).not.toContain('150');
        expect(container.textContent).not.toContain('m TD');
    });

    it('collar fetch uses same-origin credentials', async () => {
        render(<StripLogViewer holeId="DH-001" projectId="proj-abc" />);
        await waitFor(() => expect(fetchSpy).toHaveBeenCalled());

        const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
        expect(init?.credentials).toBe('same-origin');
        const headers = (init?.headers ?? {}) as Record<string, string>;
        expect(headers['Authorization']).toBeUndefined();
    });

    it('resolves the hole server-side with hole_id and per_page=1, not by paging the whole index', async () => {
        fetchSpy.mockImplementation(
            async (url: RequestInfo | URL) =>
                new Response(
                    JSON.stringify({ data: String(url).includes('/collars?') ? [collarPayload] : collarPayload }),
                    { status: 200, headers: { 'Content-Type': 'application/json' } },
                ),
        );

        render(<StripLogViewer holeId="DH 001/A" projectId="proj-abc" />);
        await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(2));

        const [indexUrl] = fetchSpy.mock.calls[0] as [string];
        expect(indexUrl).toBe('/api/v1/projects/proj-abc/collars?hole_id=DH%20001%2FA&per_page=1');
        const [showUrl] = fetchSpy.mock.calls[1] as [string];
        expect(showUrl).toBe('/api/v1/projects/proj-abc/collars/col-001');
    });

    it('ignores a stale response and an aborted request after the hole changes', async () => {
        const resolvers: Array<(body: unknown) => void> = [];
        const signals: AbortSignal[] = [];
        fetchSpy.mockImplementation((url: RequestInfo | URL, init?: RequestInit) => {
            signals.push(init?.signal as AbortSignal);
            const isIndex = String(url).includes('/collars?');
            // Index calls hang until resolved by the test, so the first hole's
            // response can arrive after the second hole's.
            if (isIndex) {
                return new Promise<Response>((resolve) => {
                    resolvers.push((body) =>
                        resolve(
                            new Response(JSON.stringify({ data: body }), {
                                status: 200,
                                headers: { 'Content-Type': 'application/json' },
                            }),
                        ),
                    );
                });
            }
            const id = String(url).split('/').pop();
            return Promise.resolve(
                new Response(
                    JSON.stringify({
                        data: { ...collarPayload, collar_id: id, hole_id: id === 'col-B' ? 'DH-B' : 'DH-A' },
                    }),
                    { status: 200, headers: { 'Content-Type': 'application/json' } },
                ),
            );
        });

        const { rerender, container } = render(<StripLogViewer holeId="DH-A" projectId="proj-abc" />);
        await waitFor(() => expect(resolvers).toHaveLength(1));

        rerender(<StripLogViewer holeId="DH-B" projectId="proj-abc" />);
        await waitFor(() => expect(resolvers).toHaveLength(2));
        // The first run was aborted when the effect re-ran.
        expect(signals[0].aborted).toBe(true);
        expect(signals[1].aborted).toBe(false);

        // Second hole answers first, then the stale first-hole response lands.
        await act(async () => {
            resolvers[1]([{ ...collarPayload, collar_id: 'col-B', hole_id: 'DH-B' }]);
        });
        await waitFor(() => expect(container.textContent).toContain('DH-B'));
        await act(async () => {
            resolvers[0]([{ ...collarPayload, collar_id: 'col-A', hole_id: 'DH-A' }]);
        });

        expect(container.textContent).toContain('DH-B');
        expect(container.textContent).not.toContain('DH-A');
        // No show() call was made for the stale hole.
        const showUrls = fetchSpy.mock.calls.map(([u]) => String(u)).filter((u) => !u.includes('/collars?'));
        expect(showUrls).toEqual(['/api/v1/projects/proj-abc/collars/col-B']);
    });
});
