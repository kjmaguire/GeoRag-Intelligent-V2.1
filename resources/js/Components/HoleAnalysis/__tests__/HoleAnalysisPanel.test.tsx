/**
 * HoleAnalysisPanel.test.tsx
 *
 * Security regression guard: HoleAnalysisPanel must NOT read auth tokens from
 * localStorage. Its analysis fetch uses Sanctum session cookie via
 * `credentials: 'same-origin'` (types.ts:11-12).
 *
 * Plotly-based sub-panels (OrientationSpiral, AzimuthDipVsDepth, GeochemPlots,
 * Stereosphere) are lazy-loaded and mocked to avoid CJS-interop issues in jsdom.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';

// Mock lazy-loaded Plotly panels
vi.mock('../OrientationSpiral', () => ({ default: () => <div data-testid="spiral-stub" /> }));
vi.mock('../AzimuthDipVsDepth', () => ({ default: () => <div data-testid="azimuth-stub" /> }));
vi.mock('../GeochemPlots', () => ({ default: () => <div data-testid="geochem-stub" /> }));
vi.mock('../Stereosphere', () => ({ default: () => <div data-testid="stereosphere-stub" /> }));
vi.mock('../Stereonet', () => ({ default: () => <div data-testid="stereonet-stub" /> }));

import HoleAnalysisPanel from '../HoleAnalysisPanel';

describe('HoleAnalysisPanel — auth surface', () => {
    let getItemSpy: ReturnType<typeof vi.spyOn>;
    let fetchSpy: ReturnType<typeof vi.spyOn>;

    const analysisPayload = {
        collar: {
            collar_id: 'col-1',
            hole_id: 'DH-001',
            hole_type: 'Diamond',
            status: 'Completed',
            total_depth: 350,
            azimuth: 180,
            dip: -60,
            elevation: 420,
            easting: 500000,
            northing: 6200000,
        },
        surveys: [],
        structures: [],
        geochem: [],
    };

    beforeEach(() => {
        getItemSpy = vi.spyOn(Storage.prototype, 'getItem');
        fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
            new Response(JSON.stringify(analysisPayload), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            }),
        );
    });

    afterEach(() => {
        getItemSpy.mockRestore();
        fetchSpy.mockRestore();
    });

    it('does not read auth tokens from localStorage during analysis fetch', async () => {
        render(<HoleAnalysisPanel holeId="DH-001" projectId="proj-abc" />);
        await waitFor(() => expect(fetchSpy).toHaveBeenCalled());

        const tokenLike = /token|jwt|secret/i;
        const offending = getItemSpy.mock.calls
            .map(([key]) => String(key))
            .filter((k) => tokenLike.test(k));
        expect(offending).toEqual([]);
    });

    it('analysis fetch uses same-origin credentials', async () => {
        render(<HoleAnalysisPanel holeId="DH-001" projectId="proj-abc" />);
        await waitFor(() => expect(fetchSpy).toHaveBeenCalled());

        const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
        expect(init?.credentials).toBe('same-origin');
        const headers = (init?.headers ?? {}) as Record<string, string>;
        expect(headers['Authorization']).toBeUndefined();
    });
});
