/**
 * PublicGeoFeatureCard — the clicked-record card on the Public Geoscience map
 * (2026-09-29). Before it existed a click on a public drillhole did nothing.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import PublicGeoFeatureCard, { pgeoChunkId, pointRows, stratContacts } from '../PublicGeoFeatureCard';
import type { PolygonFeatureProperties } from '../polygonLayers';

afterEach(() => {
    vi.unstubAllGlobals();
});

describe('pgeoChunkId', () => {
    it('builds the key the PGEO citation resolver parses', () => {
        // AbstractPgeoResolver::parseChunkId: ^pg_<type>:<source_id> … pg_id=<uuid>
        expect(pgeoChunkId('drillhole_collar', 'CA-SK-DRILLHOLE', 'abc')).toBe('pg_drillhole_collar:CA-SK-DRILLHOLE:pg_id=abc');
    });
});

describe('stratContacts', () => {
    it('keeps contacts with a depth, shallowest first, labelled', () => {
        const contacts = stratContacts({
            top_crystalline_basement: { depth_m: 310.2, elevation_m: 170 },
            base_of_quaternary: { depth_m: 42.1, elevation_m: null },
            base_of_phanerozoic: { depth_m: null, elevation_m: 400 },
        });
        expect(contacts.map((c) => c.key)).toEqual(['base_of_quaternary', 'top_crystalline_basement']);
        expect(contacts[0].label).toBe('Base of Quaternary');
        expect(contacts[1].elevation_m).toBe(170);
    });

    it('is empty for anything that is not a contacts object', () => {
        expect(stratContacts(null)).toEqual([]);
        expect(stratContacts('{}')).toEqual([]);
        expect(stratContacts({ x: 'y' })).toEqual([]);
    });
});

describe('pointRows', () => {
    it('formats a drillhole like the Workspace hole card', () => {
        const rows = Object.fromEntries(
            pointRows('drillhole_collar', {
                drillhole_id: 'GOS-1',
                total_length_m: '350.5',
                inclination_deg: '-90',
                azimuth_deg: null,
                commodity_of_interest: ['uranium', 'gold'],
                core_availability: 'unknown',
            }),
        );
        expect(rows['Hole ID']).toBe('GOS-1');
        expect(rows['Total depth']).toBe('350.5 m');
        expect(rows['Dip / azimuth']).toBe('-90° / —');
        expect(rows['Commodity']).toBe('uranium, gold');
        // "unknown" is the column default, not information.
        expect(rows['Core']).toBeNull();
        expect(rows['Company']).toBeNull();
    });
});

describe('PublicGeoFeatureCard', () => {
    it('renders a polygon from the map feed without fetching', () => {
        const fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
        const props = {
            id: 'p1',
            layer: 'mineral_disposition',
            label: 'MC00012345',
            jurisdiction_code: 'CA-SK',
            source_id: 'CA-SK-MINERAL-DISPOSITION-MINING-0',
            holder_name: '<b>Holder</b> Ltd.',
            status: 'active',
        } as unknown as PolygonFeatureProperties;

        render(
            <PublicGeoFeatureCard
                selection={{ kind: 'polygon', props }}
                sources={{
                    'CA-SK-MINERAL-DISPOSITION-MINING-0': {
                        name: 'SK Mineral Tenure',
                        license_summary: 'SK Open Licence',
                        license_url: 'javascript:alert(1)',
                    },
                }}
                onClose={() => {}}
            />,
        );

        const card = screen.getByRole('dialog');
        expect(fetchMock).not.toHaveBeenCalled();
        expect(card.textContent).toContain('MC00012345');
        // Upstream text is rendered as text, never as markup.
        expect(card.textContent).toContain('<b>Holder</b> Ltd.');
        expect(card.querySelector('b')).toBeNull();
        // A non-http licence URL never becomes a link.
        expect(screen.queryByRole('link')).toBeNull();
        expect(card.textContent).toContain('SK Open Licence');
    });

    it('says so when the record has left the synced data', async () => {
        vi.stubGlobal(
            'fetch',
            vi.fn(async () =>
                new Response(
                    JSON.stringify({
                        title: null,
                        jurisdiction: { code: 'CA-SK', name: null, authority: null },
                        source: { source_id: 'CA-SK-MINE-LOC', name: null, service_url: null },
                        license: { summary: null, url: null },
                        refresh: { last_refreshed_at: null },
                        references_summary: { count: 0, documents: [] },
                        entity: null,
                    }),
                    { status: 200 },
                ),
            ),
        );
        render(
            <PublicGeoFeatureCard
                selection={{ kind: 'point', layer: 'mine', id: 'm1', sourceId: 'CA-SK-MINE-LOC', label: 'Old Mine', jurisdiction: 'CA-SK', lngLat: [-105, 55] }}
                onClose={() => {}}
            />,
        );
        expect(await screen.findByText(/no longer in the synced data/)).toBeTruthy();
        expect(screen.getByRole('dialog').textContent).toContain('Old Mine');
    });
});
