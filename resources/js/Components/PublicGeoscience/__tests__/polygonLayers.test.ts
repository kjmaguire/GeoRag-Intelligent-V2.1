import { describe, it, expect } from 'vitest';
import {
    POLYGON_COLOR_MATCH,
    POLYGON_LAYER_KEYS,
    polygonLayerStatus,
    polygonPopupHtml,
    type PolygonFeatureProperties,
} from '../polygonLayers';

const tenure: PolygonFeatureProperties = {
    id: 'p1',
    layer: 'mineral_disposition',
    label: 'MC-1234',
    jurisdiction_code: 'CA-SK',
    source_id: 'CA-SK-MINERAL-DISPOSITION-MINING-0',
    disposition_type: 'mineral',
    status: 'active',
    holder_name: '<img src=x onerror=alert(1)> Exploration Ltd',
    issue_date: '2021-03-04',
    expiry_date: null,
    area_ha: '120.50',
};

const sources = {
    'CA-SK-MINERAL-DISPOSITION-MINING-0': {
        name: 'Saskatchewan Mineral Tenure - MINING-0',
        license_summary: 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0',
        license_url: 'https://example.gov.sk.ca/licence.pdf',
    },
};

describe('polygonPopupHtml', () => {
    it('shows the key attributes, the source and a licence link', () => {
        const html = polygonPopupHtml(tenure, sources);
        expect(html).toContain('MC-1234');
        expect(html).toContain('Mineral tenure');
        expect(html).toContain('Holder:');
        expect(html).toContain('120.50');
        expect(html).toContain('Saskatchewan Mineral Tenure - MINING-0');
        expect(html).toContain('href="https://example.gov.sk.ca/licence.pdf"');
        expect(html).toContain('Standard Unrestricted Use Data License');
    });

    it('escapes upstream strings — they reach the DOM via setHTML', () => {
        const html = polygonPopupHtml(tenure, sources);
        expect(html).not.toContain('<img');
        expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;');
    });

    it('omits empty attributes instead of printing "null"', () => {
        const html = polygonPopupHtml(tenure, sources);
        expect(html).not.toContain('Good to / expiry');
        expect(html).not.toContain('null');
    });

    it('never turns a non-http licence URL into a link', () => {
        const html = polygonPopupHtml(tenure, {
            [tenure.source_id]: { name: 'x', license_summary: 'Some licence', license_url: 'javascript:alert(1)' },
        });
        expect(html).not.toContain('href=');
        expect(html).toContain('Some licence');
    });

    it('says so when the source has no licence on record', () => {
        expect(polygonPopupHtml(tenure, undefined)).toContain('licence unknown');
    });
});

describe('polygonLayerStatus', () => {
    it('asks for a closer zoom below the layer minimum', () => {
        expect(
            polygonLayerStatus({ mode: 'min_zoom', min_zoom: 6, total_in_view: null, returned: 0, truncated: false }),
        ).toBe('zoom in (≥ 6)');
    });

    it('never lets a capped layer pass for a complete one', () => {
        expect(
            polygonLayerStatus({ mode: 'polygons', min_zoom: 6, total_in_view: 30906, returned: 1500, truncated: true }),
        ).toBe(`${(1500).toLocaleString()} of ${(30906).toLocaleString()} — zoom in for all`);
    });

    it('reports a complete layer and an empty one', () => {
        expect(
            polygonLayerStatus({ mode: 'polygons', min_zoom: 3, total_in_view: 12, returned: 12, truncated: false }),
        ).toBe('12 in view');
        expect(
            polygonLayerStatus({ mode: 'polygons', min_zoom: 3, total_in_view: 0, returned: 0, truncated: false }),
        ).toBe('none in view');
        expect(polygonLayerStatus(undefined)).toBeNull();
    });
});

it('colours every polygon layer', () => {
    for (const key of POLYGON_LAYER_KEYS) {
        expect(POLYGON_COLOR_MATCH).toContain(key);
    }
});
