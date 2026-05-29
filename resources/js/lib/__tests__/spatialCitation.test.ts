/**
 * Phase G.4 — tests for parseSpatialCitation + evidenceMapStore.
 */
import { afterEach, describe, expect, it } from 'vitest';

import { parseSpatialCitation, isSpatialCitation } from '../spatialCitation';
import { evidenceMapStore } from '../evidenceMapStore';

describe('parseSpatialCitation', () => {
    it('returns null for an empty / missing source_chunk_id', () => {
        expect(parseSpatialCitation(null)).toBeNull();
        expect(parseSpatialCitation(undefined)).toBeNull();
        expect(parseSpatialCitation({ source_chunk_id: '' })).toBeNull();
        expect(parseSpatialCitation({} as { source_chunk_id?: string })).toBeNull();
    });

    it('parses a downhole-logs source as hole_id', () => {
        const pin = parseSpatialCitation({
            source_chunk_id: 'silver.lithology_logs:hole=36-1042:collar=abc-uuid:intervals=120',
        });
        expect(pin).toEqual({ kind: 'hole_id', hole_id: '36-1042' });
    });

    it('parses a spatial-collars source with first= as collar_set', () => {
        const pin = parseSpatialCitation({
            source_chunk_id: 'silver.collars:count=63:first=abc-def-1234',
        });
        expect(pin).toEqual({ kind: 'collar_set', first_collar_id: 'abc-def-1234' });
    });

    it('returns null for an empty silver.collars (count=0)', () => {
        const pin = parseSpatialCitation({
            source_chunk_id: 'silver.collars:count=0',
        });
        expect(pin).toBeNull();
    });

    it('parses a PG feature source', () => {
        const pin = parseSpatialCitation({
            source_chunk_id: 'pg_drillhole_collar:CA-SK:feature=PLS-19-001:pg_id=abc-uuid',
        });
        expect(pin).toEqual({
            kind: 'pg_feature',
            canonical_type: 'drillhole_collar',
            feature_id: 'PLS-19-001',
        });
    });

    it('skips PG features whose feature_id is the "unknown" sentinel', () => {
        const pin = parseSpatialCitation({
            source_chunk_id: 'pg_mineral_occurrence:CA-SK:feature=unknown:pg_id=abc-uuid',
        });
        expect(pin).toBeNull();
    });

    it('returns null for assay / project / neo4j / qdrant sources', () => {
        for (const src of [
            'silver.samples:element=U3O8_ppm:count=10',
            'silver.projects:slug=cameco-shirley-basin:company=CAMECO RESOURCES:curves=16',
            'neo4j:entities=20:first=abc-uuid',
            'georag_reports:abc:section=Section 14:chunk=def',
            'no-tool-call',
        ]) {
            expect(
                parseSpatialCitation({ source_chunk_id: src }),
                `expected null for ${src}`,
            ).toBeNull();
        }
    });

    it('isSpatialCitation returns true only when parseSpatialCitation does', () => {
        expect(isSpatialCitation({
            source_chunk_id: 'silver.lithology_logs:hole=PLS-22-08:intervals=12',
        })).toBe(true);
        expect(isSpatialCitation({
            source_chunk_id: 'silver.samples:element=U3O8_ppm:count=0',
        })).toBe(false);
    });
});

describe('evidenceMapStore', () => {
    afterEach(() => evidenceMapStore._reset());

    it('starts empty', () => {
        expect(evidenceMapStore.get()).toBeNull();
    });

    it('set + get round-trips', () => {
        evidenceMapStore.set({ kind: 'hole_id', hole_id: '36-1042' });
        expect(evidenceMapStore.get()).toEqual({
            kind: 'hole_id', hole_id: '36-1042',
        });
    });

    it('clear() resets the pin', () => {
        evidenceMapStore.set({ kind: 'hole_id', hole_id: '36-1042' });
        evidenceMapStore.clear();
        expect(evidenceMapStore.get()).toBeNull();
    });

    it('notifies subscribers on set', () => {
        let calls = 0;
        const unsubscribe = evidenceMapStore.subscribe(() => { calls++; });
        evidenceMapStore.set({ kind: 'hole_id', hole_id: 'a' });
        evidenceMapStore.set({ kind: 'hole_id', hole_id: 'b' });
        expect(calls).toBe(2);
        unsubscribe();
        evidenceMapStore.set({ kind: 'hole_id', hole_id: 'c' });
        expect(calls).toBe(2);  // unsubscribed
    });

    it('skips notification when setting the same pin', () => {
        let calls = 0;
        evidenceMapStore.subscribe(() => { calls++; });
        const same = { kind: 'hole_id' as const, hole_id: 'a' };
        evidenceMapStore.set(same);
        evidenceMapStore.set({ kind: 'hole_id', hole_id: 'a' });
        expect(calls).toBe(1);  // structural-equal pins coalesce
    });

    it('clear() is a no-op when already clear', () => {
        let calls = 0;
        evidenceMapStore.subscribe(() => { calls++; });
        evidenceMapStore.clear();
        evidenceMapStore.clear();
        expect(calls).toBe(0);
    });
});
