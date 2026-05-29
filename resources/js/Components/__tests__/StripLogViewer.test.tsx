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

    it('collar fetch uses same-origin credentials', async () => {
        render(<StripLogViewer holeId="DH-001" projectId="proj-abc" />);
        await waitFor(() => expect(fetchSpy).toHaveBeenCalled());

        const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
        expect(init?.credentials).toBe('same-origin');
        const headers = (init?.headers ?? {}) as Record<string, string>;
        expect(headers['Authorization']).toBeUndefined();
    });
});
