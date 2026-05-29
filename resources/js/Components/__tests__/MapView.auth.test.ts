/**
 * MapView.auth.test.ts
 *
 * Security regression guard: MapView must NOT read auth tokens from localStorage.
 * Its fetchCollars path uses Sanctum session cookie via `credentials: 'same-origin'`
 * (types.ts:11-12). The MapLibre `transformRequest` callback is intentionally
 * excluded — it is MapLibre-internal and not exercisable in jsdom.
 *
 * Approach: static source inspection via Vite's `?raw` import (no jsdom render).
 * MapView imports maplibre-gl which requires WebGL/canvas context unavailable in
 * jsdom. Source inspection is the sanctioned fallback per the task spec and is a
 * direct assertion on the literal source text — sufficient as a regression guard
 * for this single-author controlled file.
 */

import { describe, it, expect } from 'vitest';
import mapViewSource from '../MapView.tsx?raw';

describe('MapView — auth surface (static source inspection)', () => {
    it('source does not contain localStorage.getItem with a token-like key', () => {
        // Match any literal string argument to localStorage.getItem that contains
        // "token", "jwt", or "secret" (case-insensitive).
        const tokenRead = /localStorage\.getItem\(['"][^'"]*(?:token|jwt|secret)[^'"]*['"]/i;
        expect(tokenRead.test(mapViewSource)).toBe(false);
    });

    it('fetchCollars fetch call carries credentials same-origin', () => {
        // The source must contain `credentials: 'same-origin'` in the fetchCollars block.
        // Lightweight lint that the Sanctum cookie pattern is present.
        expect(mapViewSource).toMatch(/credentials:\s*['"]same-origin['"]/);
    });

    it('source does not contain Authorization header referencing localStorage', () => {
        // Guard against patterns like: Authorization: `Bearer ${localStorage.getItem(...)}`
        const authFromStorage = /Authorization[^;]*localStorage\.getItem/;
        expect(authFromStorage.test(mapViewSource)).toBe(false);
    });
});
