/**
 * V1.5-11 — Persistent storage for MapView's per-layer visibility state.
 *
 * Module 8 Chunk 8.7 added a `Record<string, boolean>` of MVT layer
 * visibility toggles. The original implementation only kept state in
 * React for the lifetime of the page; a refresh reset every toggle to
 * the default. This module persists and restores that state in
 * localStorage so a user's preferred view (e.g. seismic + geochem off,
 * collars + drill traces on) survives reloads.
 *
 * Storage key
 * -----------
 * `georag:map_layer_visibility:v1` — version-prefixed so we can ship a
 * non-backwards-compatible layer-set change later (just bump the suffix
 * to v2 and the old prefs are silently abandoned, defaults take over).
 *
 * Failure semantics
 * -----------------
 * localStorage may be unavailable (private browsing, quota exceeded,
 * SSR contexts). Every helper catches the resulting DOMException and
 * falls back to the default — never throws. `read()` returns null on
 * miss so the caller can fall back to MVT_DEFAULT_VISIBILITY.
 *
 * Schema
 * ------
 * The stored value is a JSON object: `{ <layerId>: boolean }`. Unknown
 * layer IDs (from a stale storage entry that pre-dates a layer addition)
 * are dropped at read time; missing layer IDs (added since the prefs
 * were saved) get the default visibility.
 */

const STORAGE_KEY = 'georag:map_layer_visibility:v1';

export type LayerVisibility = Record<string, boolean>;

/**
 * Read the persisted visibility map. Returns null when nothing is
 * stored OR localStorage is unavailable; the caller should fall back
 * to its default.
 */
export function readLayerVisibility(): LayerVisibility | null {
    if (typeof window === 'undefined' || typeof window.localStorage === 'undefined') {
        return null;
    }
    try {
        const raw = window.localStorage.getItem(STORAGE_KEY);
        if (raw === null) return null;
        const parsed: unknown = JSON.parse(raw);
        if (
            parsed === null
            || typeof parsed !== 'object'
            || Array.isArray(parsed)
        ) {
            return null;
        }
        // Filter to boolean values only — guards against tampered storage.
        const out: LayerVisibility = {};
        for (const [key, value] of Object.entries(parsed)) {
            if (typeof value === 'boolean') out[key] = value;
        }
        return out;
    } catch {
        return null;
    }
}

/**
 * Persist the visibility map. Best-effort; storage failures are
 * swallowed so the toggle still updates in-memory React state even
 * if it can't persist.
 */
export function writeLayerVisibility(visibility: LayerVisibility): void {
    if (typeof window === 'undefined' || typeof window.localStorage === 'undefined') {
        return;
    }
    try {
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify(visibility));
    } catch {
        // QuotaExceededError, private-browsing block, etc. — swallow.
    }
}

/**
 * Merge persisted visibility into the default map. Unknown stored keys
 * are dropped; missing keys take the default value. Caller passes the
 * default and the persisted value (which may be null on miss).
 */
export function mergeLayerVisibility(
    defaults: LayerVisibility,
    persisted: LayerVisibility | null,
): LayerVisibility {
    if (persisted === null) return { ...defaults };
    const merged: LayerVisibility = { ...defaults };
    for (const key of Object.keys(merged)) {
        if (Object.prototype.hasOwnProperty.call(persisted, key)) {
            merged[key] = persisted[key];
        }
    }
    return merged;
}
