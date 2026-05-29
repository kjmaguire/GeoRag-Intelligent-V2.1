/**
 * MapView tile URL builder tests — Module 8 Chunks 8.5 + 8.6.
 *
 * Tests the standalone tileUrl helper and the MVT layer registry.
 * MapLibre is NOT rendered — all tests are pure data / function tests.
 * The map.getSource interface is mocked where needed.
 */
import { describe, it, expect, vi } from 'vitest';
import { buildSilverTileUrl, buildAllSilverTileUrls } from '../../lib/tileUrl';
import { MVT_LAYERS } from '../../lib/mvtLayers';

// ─── URL builder shape ─────────────────────────────────────────────────────

describe('buildSilverTileUrl', () => {
    const PROJECT_UUID = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890';

    it('produces the correct /tiles/silver/ path pattern', () => {
        const url = buildSilverTileUrl('pg_collars_by_project', PROJECT_UUID, 0);
        // URL is now absolute (origin-prefixed) so MapLibre's tile worker can
        // parse it from a Web Worker context. The path + query must still match.
        expect(url).toContain(
            `/tiles/silver/pg_collars_by_project/{z}/{x}/{y}.pbf?project_id=${PROJECT_UUID}&v=0`,
        );
    });

    it('includes the v= cache-bust suffix when data_version is non-zero', () => {
        const url = buildSilverTileUrl('pg_collars_by_project', PROJECT_UUID, 42);
        expect(url).toContain('&v=42');
        expect(url).not.toContain('&v=0');
    });

    it('uses v=0 fallback when data_version is 0', () => {
        const url = buildSilverTileUrl('pg_drill_traces_by_project', PROJECT_UUID, 0);
        expect(url).toContain('&v=0');
    });

    it('embeds project_id as a query param', () => {
        const uuid = 'deadbeef-dead-beef-dead-beefdeadbeef';
        const url = buildSilverTileUrl('pg_seismic_by_project', uuid, 7);
        expect(url).toContain(`project_id=${uuid}`);
    });

    it('preserves {z}/{x}/{y} placeholders for MapLibre template substitution', () => {
        const url = buildSilverTileUrl('pg_geochem_by_project', PROJECT_UUID, 1);
        expect(url).toContain('{z}/{x}/{y}.pbf');
    });

    it('does not include a legacy /{projectId}/ path segment', () => {
        const url = buildSilverTileUrl('pg_collars_by_project', PROJECT_UUID, 3);
        // Old stale pattern was /tiles/${projectId}/${functionName}/...
        // New pattern must NOT have project UUID in the path — only in query string.
        expect(url).not.toMatch(new RegExp(`/tiles/${PROJECT_UUID}/`));
        // Absolute origin-prefixed URL — match anywhere, not just at start.
        expect(url).toMatch(/\/tiles\/silver\//);
    });
});

// ─── All-layers builder ────────────────────────────────────────────────────

describe('buildAllSilverTileUrls', () => {
    const PROJECT_UUID = 'ffffffff-ffff-ffff-ffff-ffffffffffff';

    it('returns one URL per layer in the provided array', () => {
        const urls = buildAllSilverTileUrls(MVT_LAYERS, PROJECT_UUID, 5);
        expect(urls.size).toBe(MVT_LAYERS.length);
    });

    it('keys are the layer id fields', () => {
        const urls = buildAllSilverTileUrls(MVT_LAYERS, PROJECT_UUID, 0);
        for (const layer of MVT_LAYERS) {
            expect(urls.has(layer.id)).toBe(true);
        }
    });

    it('all URLs contain the project_id query param', () => {
        const urls = buildAllSilverTileUrls(MVT_LAYERS, PROJECT_UUID, 0);
        for (const [, url] of urls) {
            expect(url).toContain(`project_id=${PROJECT_UUID}`);
        }
    });
});

// ─── Each of the 7 silver functions maps to its correct full function name ─

describe('MVT_LAYERS registry — full function names', () => {
    const getFunctionName = (id: string) => {
        const layer = MVT_LAYERS.find((l) => l.id === id);
        if (!layer) throw new Error(`Layer '${id}' not found in MVT_LAYERS`);
        return layer.functionName;
    };

    it('collars layer uses pg_collars_by_project', () => {
        expect(getFunctionName('collars')).toBe('pg_collars_by_project');
    });

    it('traces layer uses pg_drill_traces_by_project', () => {
        expect(getFunctionName('traces')).toBe('pg_drill_traces_by_project');
    });

    it('boundaries layer uses pg_boundaries_by_project', () => {
        expect(getFunctionName('boundaries')).toBe('pg_boundaries_by_project');
    });

    it('formations layer uses pg_formations_by_project', () => {
        expect(getFunctionName('formations')).toBe('pg_formations_by_project');
    });

    it('historic-workings layer uses pg_historic_workings_by_project', () => {
        expect(getFunctionName('historic-workings')).toBe('pg_historic_workings_by_project');
    });

    it('seismic layer uses pg_seismic_by_project', () => {
        expect(getFunctionName('seismic')).toBe('pg_seismic_by_project');
    });

    it('geochem layer uses pg_geochem_by_project', () => {
        expect(getFunctionName('geochem')).toBe('pg_geochem_by_project');
    });
});

// ─── data_version change → setTiles called ────────────────────────────────

describe('data_version cache-bust — mock map.getSource().setTiles', () => {
    const PROJECT_UUID = '11111111-2222-3333-4444-555555555555';

    /**
     * Build a minimal mock of MapLibre map.getSource() that tracks setTiles calls.
     * Returns a map where each sourceId has its own setTiles spy.
     */
    function buildMockMap(layerIds: string[]) {
        // Use plain jest-compatible spy type via vi.fn() — kept as 'any' to avoid
        // MockInstance generic arity issues across vitest versions.
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const setTilesSpies: Map<string, any> = new Map();
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const sources: Map<string, { setTiles: (...args: any[]) => void }> = new Map();

        for (const id of layerIds) {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const spy: any = vi.fn();
            setTilesSpies.set(id, spy);
            sources.set(id, { setTiles: spy });
        }

        const mockMap = {
            getSource: (id: string) => sources.get(id) ?? null,
        };

        return { mockMap, setTilesSpies };
    }

    it('calls setTiles once per MVT source when data_version changes', () => {
        const sourceIds = MVT_LAYERS.map((l) => `mvt-${l.id}-source`);
        const { mockMap, setTilesSpies } = buildMockMap(sourceIds);
        const newVersion = 7;

        // Simulate the useEffect body: for each layer, call setTiles with new URL
        for (const layer of MVT_LAYERS) {
            const sourceId = `mvt-${layer.id}-source`;
            const source = mockMap.getSource(sourceId);
            if (source && typeof source.setTiles === 'function') {
                source.setTiles([buildSilverTileUrl(layer.functionName, PROJECT_UUID, newVersion)]);
            }
        }

        // Every source should have been called exactly once
        for (const [, spy] of setTilesSpies) {
            expect(spy).toHaveBeenCalledTimes(1);
        }
    });

    it('setTiles is called with a URL containing the new data_version', () => {
        const sourceIds = MVT_LAYERS.map((l) => `mvt-${l.id}-source`);
        const { mockMap, setTilesSpies } = buildMockMap(sourceIds);
        const newVersion = 99;

        for (const layer of MVT_LAYERS) {
            const sourceId = `mvt-${layer.id}-source`;
            const source = mockMap.getSource(sourceId);
            source?.setTiles([buildSilverTileUrl(layer.functionName, PROJECT_UUID, newVersion)]);
        }

        // All calls should include v=99
        for (const [, spy] of setTilesSpies) {
            const [urlArgs] = spy.mock.calls[0];
            expect(urlArgs[0]).toContain('&v=99');
        }
    });
});
