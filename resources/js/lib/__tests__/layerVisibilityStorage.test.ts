/**
 * V1.5-11 — Tests for layerVisibilityStorage.
 *
 * Covers read / write / merge cycles + the failure paths
 * (localStorage unavailable, malformed JSON, tampered values).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
    mergeLayerVisibility,
    readLayerVisibility,
    writeLayerVisibility,
} from '../layerVisibilityStorage';

const KEY = 'georag:map_layer_visibility:v1';
const DEFAULTS = {
    collars: true,
    drill_traces: true,
    seismic: false,
};

beforeEach(() => {
    window.localStorage.clear();
});

afterEach(() => {
    vi.restoreAllMocks();
});

describe('readLayerVisibility', () => {
    it('returns null when nothing is stored', () => {
        expect(readLayerVisibility()).toBeNull();
    });

    it('reads a previously-written object', () => {
        window.localStorage.setItem(KEY, JSON.stringify({ collars: false, seismic: true }));
        expect(readLayerVisibility()).toEqual({ collars: false, seismic: true });
    });

    it('returns null on malformed JSON', () => {
        window.localStorage.setItem(KEY, '{not json');
        expect(readLayerVisibility()).toBeNull();
    });

    it('returns null when stored value is an array', () => {
        window.localStorage.setItem(KEY, JSON.stringify(['unexpected']));
        expect(readLayerVisibility()).toBeNull();
    });

    it('drops non-boolean values from a tampered store', () => {
        window.localStorage.setItem(
            KEY,
            JSON.stringify({ collars: true, seismic: 'truthy?', extra: 1 }),
        );
        expect(readLayerVisibility()).toEqual({ collars: true });
    });

    it('returns null when localStorage throws on getItem', () => {
        vi.spyOn(window.localStorage.__proto__, 'getItem').mockImplementation(() => {
            throw new DOMException('SecurityError');
        });
        expect(readLayerVisibility()).toBeNull();
    });
});

describe('writeLayerVisibility', () => {
    it('round-trips with readLayerVisibility', () => {
        writeLayerVisibility({ collars: false, drill_traces: true });
        expect(readLayerVisibility()).toEqual({ collars: false, drill_traces: true });
    });

    it('swallows quota errors silently', () => {
        vi.spyOn(window.localStorage.__proto__, 'setItem').mockImplementation(() => {
            throw new DOMException('QuotaExceededError');
        });
        // Doesn't throw.
        expect(() => writeLayerVisibility({ collars: true })).not.toThrow();
    });
});

describe('mergeLayerVisibility', () => {
    it('returns a fresh copy of defaults when persisted is null', () => {
        const merged = mergeLayerVisibility(DEFAULTS, null);
        expect(merged).toEqual(DEFAULTS);
        // Make sure we don't return the same reference (defensive).
        expect(merged).not.toBe(DEFAULTS);
    });

    it('overlays persisted values onto defaults', () => {
        const merged = mergeLayerVisibility(DEFAULTS, { collars: false, seismic: true });
        expect(merged).toEqual({
            collars: false,        // overridden
            drill_traces: true,    // unchanged from default
            seismic: true,         // overridden
        });
    });

    it('drops persisted keys not present in defaults', () => {
        // Stale layer ID `pg_obsolete` from an earlier deploy — should NOT
        // leak into the merged map.
        const merged = mergeLayerVisibility(DEFAULTS, {
            collars: false,
            pg_obsolete: true,
        });
        expect(merged).toEqual({
            collars: false,
            drill_traces: true,
            seismic: false,
        });
        expect('pg_obsolete' in merged).toBe(false);
    });

    it('uses default for keys missing from persisted (new layer addition)', () => {
        // A layer added since the prefs were saved — gets its default value.
        const persistedFromPrevDeploy = { collars: false };
        const merged = mergeLayerVisibility(DEFAULTS, persistedFromPrevDeploy);
        expect(merged.drill_traces).toBe(true);
        expect(merged.seismic).toBe(false);
    });
});
