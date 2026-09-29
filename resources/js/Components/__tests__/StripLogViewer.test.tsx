/**
 * StripLogViewer.test.tsx
 *
 * Security regression guard: StripLogViewer must NOT read auth tokens from
 * localStorage. Its two fetch calls (collar index + collar detail with
 * lithology/well_log_curves) use Sanctum session cookie via
 * `credentials: 'same-origin'` (types.ts:11-12).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';
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
        const offending = getItemSpy.mock.calls
            .map(([key]) => String(key))
            .filter((k) => tokenLike.test(k));
        expect(offending).toEqual([]);
    });

    it('a collar with no total depth is scaled to its deepest interval, not to zero (§04e 2026-09-29)', async () => {
        const noTd = {
            ...collarPayload,
            total_depth: null,
            lithology_logs: [
                { log_id: 'l1', from_depth: 0, to_depth: 120, lithology_code: 'SST' },
            ],
        };
        // First the collar index (a list), then the collar itself.
        fetchSpy.mockImplementation(async (url: RequestInfo | URL) => new Response(
            JSON.stringify({ data: String(url).includes('?per_page') ? [noTd] : noTd }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
        ));

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
});
