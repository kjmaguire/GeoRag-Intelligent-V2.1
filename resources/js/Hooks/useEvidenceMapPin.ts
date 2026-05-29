/**
 * Phase G.4 — React hook for the Evidence Map Mode store.
 *
 * Components subscribe via `useEvidenceMapPin()`; chat surface calls
 * `setEvidenceMapPin(pin)` to broadcast a click.
 */
import { useSyncExternalStore } from 'react';

import { evidenceMapStore } from '@/lib/evidenceMapStore';
import type { SpatialPin } from '@/lib/spatialCitation';

export function useEvidenceMapPin(): SpatialPin | null {
    return useSyncExternalStore(
        evidenceMapStore.subscribe,
        evidenceMapStore.get,
        // No SSR — return null on the server snapshot path.
        () => null,
    );
}

export function setEvidenceMapPin(pin: SpatialPin | null): void {
    evidenceMapStore.set(pin);
}

export function clearEvidenceMapPin(): void {
    evidenceMapStore.clear();
}
