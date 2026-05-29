/**
 * Phase G.4 — Evidence Map Mode pub-sub store.
 *
 * Minimal in-memory store that lets the chat surface tell the map surface
 * "highlight this feature." No external state-management dep — just a
 * subscribe / get / set tuple wired into React 19's
 * `useSyncExternalStore` via the matching hook.
 *
 * Pinned state is a single `SpatialPin | null`. Setting a new pin
 * replaces the previous one; the map surface re-renders to reflect it.
 *
 * Why a vanilla store rather than React context:
 *   * The chat surface and the map surface aren't always in the same
 *     Inertia page tree — Chat may be in a drawer while MapView lives
 *     in Explorer. A module-scope singleton lets them coordinate
 *     without forcing a shared provider above both.
 *   * SSR safety isn't relevant — Inertia is client-rendered.
 */
import type { SpatialPin } from './spatialCitation';

type Listener = () => void;

let currentPin: SpatialPin | null = null;
const listeners = new Set<Listener>();

export const evidenceMapStore = {
    /** Returns the currently-pinned spatial entity (or null). */
    get(): SpatialPin | null {
        return currentPin;
    },

    /** Replaces the current pin; notifies all subscribers. */
    set(pin: SpatialPin | null): void {
        if (currentPin === pin) return;
        // Equality check: structural for hole_id / collar_set / pg_feature.
        if (
            currentPin &&
            pin &&
            currentPin.kind === pin.kind &&
            JSON.stringify(currentPin) === JSON.stringify(pin)
        ) {
            return;
        }
        currentPin = pin;
        for (const l of listeners) l();
    },

    /** Clears any active pin. */
    clear(): void {
        if (currentPin === null) return;
        currentPin = null;
        for (const l of listeners) l();
    },

    /**
     * useSyncExternalStore-compatible subscribe. The hook
     * `useEvidenceMapPin` (see Hooks/useEvidenceMapPin.ts) wraps this.
     */
    subscribe(listener: Listener): () => void {
        listeners.add(listener);
        return () => {
            listeners.delete(listener);
        };
    },

    /** Test helper — wipe state + listeners between cases. */
    _reset(): void {
        currentPin = null;
        listeners.clear();
    },
};
