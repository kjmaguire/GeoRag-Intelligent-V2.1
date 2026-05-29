/**
 * Tile failure watchdog tests — Module 8 Chunk 8.7 (MAPVIEW-03).
 *
 * Tests the pure-function createTileFailureWatchdog helper.
 * No MapLibre, no React. All tests operate on the watchdog state machine.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createTileFailureWatchdog, FAILURE_THRESHOLD, WINDOW_MS } from '../tileFailureWatchdog';

describe('createTileFailureWatchdog', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    let onThreshold: (sourceId: string, count: number, urlPrefix: string) => void;
    let mockNow: () => number;
    let currentTime: number;

    beforeEach(() => {
        currentTime = 1_000_000; // arbitrary fixed start
        onThreshold = vi.fn() as unknown as (sourceId: string, count: number, urlPrefix: string) => void;
        mockNow = vi.fn(() => currentTime) as unknown as () => number;
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    // ── Test 1: 1 failure → no toast ──────────────────────────────────────────
    it('1 failure within 30s does NOT fire onThreshold', () => {
        const { recordFailure } = createTileFailureWatchdog({ onThreshold, now: mockNow });

        recordFailure('mvt-collars-source');

        expect(onThreshold).not.toHaveBeenCalled();
    });

    // ── Test 2: 2 failures → no toast ────────────────────────────────────────
    it('2 failures within 30s do NOT fire onThreshold', () => {
        const { recordFailure } = createTileFailureWatchdog({ onThreshold, now: mockNow });

        recordFailure('mvt-collars-source');
        currentTime += 5_000;
        recordFailure('mvt-collars-source');

        expect(onThreshold).not.toHaveBeenCalled();
    });

    // ── Test 3: 3 failures same source → 1 toast ─────────────────────────────
    it(`${FAILURE_THRESHOLD} failures for same source within ${WINDOW_MS}ms fires onThreshold exactly once`, () => {
        const { recordFailure } = createTileFailureWatchdog({ onThreshold, now: mockNow });

        recordFailure('mvt-collars-source', '/tiles/silver/ [source: mvt-collars-source]');
        currentTime += 5_000;
        recordFailure('mvt-collars-source', '/tiles/silver/ [source: mvt-collars-source]');
        currentTime += 5_000;
        recordFailure('mvt-collars-source', '/tiles/silver/ [source: mvt-collars-source]');

        expect(onThreshold).toHaveBeenCalledTimes(1);
    });

    // ── Test 4: 3 failures across DIFFERENT sources → no toast ───────────────
    it('3 failures across different sources do NOT fire onThreshold (per-source threshold)', () => {
        const { recordFailure } = createTileFailureWatchdog({ onThreshold, now: mockNow });

        recordFailure('mvt-collars-source');
        recordFailure('mvt-seismic-source');
        recordFailure('mvt-geochem-source');

        expect(onThreshold).not.toHaveBeenCalled();
    });

    // ── Test 5: Toast includes sourceId and urlPrefix ─────────────────────────
    it('toast callback receives sourceId, count, and urlPrefix', () => {
        const { recordFailure } = createTileFailureWatchdog({ onThreshold, now: mockNow });
        const sourceId = 'mvt-formations-source';
        const urlPrefix = '/tiles/silver/ [source: mvt-formations-source]';

        recordFailure(sourceId, urlPrefix);
        recordFailure(sourceId, urlPrefix);
        recordFailure(sourceId, urlPrefix);

        expect(onThreshold).toHaveBeenCalledWith(sourceId, FAILURE_THRESHOLD, urlPrefix);
    });

    // ── Test 6: After toast fires, counter resets — no double toast ───────────
    it('counter resets after threshold fires — subsequent failures start a fresh window', () => {
        const { recordFailure } = createTileFailureWatchdog({ onThreshold, now: mockNow });

        // First breach
        for (let i = 0; i < FAILURE_THRESHOLD; i++) {
            recordFailure('mvt-traces-source');
            currentTime += 1_000;
        }
        expect(onThreshold).toHaveBeenCalledTimes(1);

        // Fresh failures after reset — should not fire a second toast until threshold again
        recordFailure('mvt-traces-source');
        recordFailure('mvt-traces-source');
        expect(onThreshold).toHaveBeenCalledTimes(1); // still 1 — only 2 new failures

        // Third failure crosses threshold again
        recordFailure('mvt-traces-source');
        expect(onThreshold).toHaveBeenCalledTimes(2);
    });

    // ── Test 7: 204 / success resets the failure counter ─────────────────────
    it('recordSuccess resets failure counter so threshold is not reached', () => {
        const { recordFailure, recordSuccess } = createTileFailureWatchdog({ onThreshold, now: mockNow });

        recordFailure('mvt-collars-source');
        recordFailure('mvt-collars-source');
        // Successful tile load — resets counter
        recordSuccess('mvt-collars-source');
        // This failure should be the FIRST in a new window (counter was reset)
        recordFailure('mvt-collars-source');

        // Total effective failures = 1 (reset was applied) — below threshold
        expect(onThreshold).not.toHaveBeenCalled();
    });

    // ── Test 8: Window expiry resets counter ──────────────────────────────────
    it('failures beyond the 30s window do not contribute to the same threshold breach', () => {
        const { recordFailure } = createTileFailureWatchdog({ onThreshold, now: mockNow });

        recordFailure('mvt-boundaries-source');
        recordFailure('mvt-boundaries-source');
        // Advance time past the window
        currentTime += WINDOW_MS + 1_000;
        // These 2 failures are in a new window — below threshold
        recordFailure('mvt-boundaries-source');
        recordFailure('mvt-boundaries-source');

        expect(onThreshold).not.toHaveBeenCalled();
    });

    // ── Test 9: getEntry reflects accumulated state ────────────────────────────
    it('getEntry returns current failure count before threshold', () => {
        const { recordFailure, getEntry } = createTileFailureWatchdog({ onThreshold, now: mockNow });

        recordFailure('mvt-seismic-source');
        recordFailure('mvt-seismic-source');

        const entry = getEntry('mvt-seismic-source');
        expect(entry).toBeDefined();
        expect(entry!.failures).toBe(2);
        expect(entry!.sourceId).toBe('mvt-seismic-source');
    });

    // ── Test 10: reset() clears all state ─────────────────────────────────────
    it('reset() clears all tracked sources', () => {
        const { recordFailure, getEntry, reset } = createTileFailureWatchdog({ onThreshold, now: mockNow });

        recordFailure('mvt-collars-source');
        recordFailure('mvt-geochem-source');
        reset();

        expect(getEntry('mvt-collars-source')).toBeUndefined();
        expect(getEntry('mvt-geochem-source')).toBeUndefined();
    });
});
