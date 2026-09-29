import { escapeHtml } from '@/lib/escapeHtml';

/**
 * Polygon overlays for the Public Geo page — the four MULTIPOLYGON public_geo
 * tables, served by GET /api/v1/public-geoscience/map?layers=… in a separate
 * `polygons` FeatureCollection (see PublicGeoscienceMapController). Kept in
 * its own module so the popup HTML (which is set with setHTML, i.e. raw HTML)
 * is pure and unit-tested for escaping.
 */

export type PolygonLayerKey =
    | 'mineral_disposition'
    | 'resource_potential_zone'
    | 'assessment_survey'
    | 'bedrock_geology';

export const POLYGON_LAYER_KEYS: PolygonLayerKey[] = [
    'mineral_disposition',
    'resource_potential_zone',
    'assessment_survey',
    'bedrock_geology',
];

export const POLYGON_LAYER_LABELS: Record<PolygonLayerKey, string> = {
    mineral_disposition: 'Mineral tenure',
    resource_potential_zone: 'Resource potential',
    assessment_survey: 'Assessment surveys',
    bedrock_geology: 'Bedrock geology',
};

export const POLYGON_LAYER_COLORS: Record<PolygonLayerKey, string> = {
    mineral_disposition: '#22c55e',
    resource_potential_zone: '#ec4899',
    assessment_survey: '#14b8a6',
    bedrock_geology: '#94a3b8',
};

/** Tenure is the one a geologist wants first; the rest are opt-in. */
export const DEFAULT_POLYGON_LAYERS: PolygonLayerKey[] = ['mineral_disposition'];

/**
 * Rows per layer: property key → label. Order is display order. The map's
 * polygon card (PublicGeoFeatureCard) and polygonPopupHtml both read it.
 */
export const POLYGON_FIELDS: Record<PolygonLayerKey, Array<[string, string]>> = {
    mineral_disposition: [
        ['disposition_type', 'Type'],
        ['status', 'Status'],
        ['holder_name', 'Holder'],
        ['issue_date', 'Issued'],
        ['expiry_date', 'Good to / expiry'],
        ['area_ha', 'Area (ha)'],
    ],
    resource_potential_zone: [
        ['commodity', 'Commodity'],
        ['potential_rank', 'Potential rank'],
        ['methodology_ref', 'Methodology'],
    ],
    assessment_survey: [
        ['survey_type', 'Survey type'],
        ['file_number', 'Assessment file'],
        ['company', 'Company'],
    ],
    bedrock_geology: [
        ['unit_code', 'Unit'],
        ['period', 'Period'],
        ['group_name', 'Group / suite'],
        ['formation', 'Formation'],
        ['lithology', 'Lithology'],
        ['scale', 'Scale'],
    ],
};

export interface PolygonFeatureProperties {
    id: string;
    layer: PolygonLayerKey;
    label: string | null;
    jurisdiction_code: string;
    source_id: string;
    [attribute: string]: string | null;
}

export interface PolygonLayerMeta {
    mode: 'polygons' | 'min_zoom';
    min_zoom: number;
    total_in_view: number | null;
    returned: number;
    truncated: boolean;
}

export interface SourceAttribution {
    name: string | null;
    license_summary: string | null;
    license_url: string | null;
}

export interface PolygonResponseFields {
    polygons?: { type: 'FeatureCollection'; features: GeoJSON.Feature[] };
    polygon_layers?: Partial<Record<PolygonLayerKey, PolygonLayerMeta>>;
    sources?: Record<string, SourceAttribution>;
}

/** MapLibre `match` expression colouring a feature by its `layer` property. */
export const POLYGON_COLOR_MATCH = [
    'match',
    ['get', 'layer'],
    ...POLYGON_LAYER_KEYS.flatMap((k) => [k, POLYGON_LAYER_COLORS[k]]),
    '#9ca3af',
];

/** Only an http(s) URL may become a link — a licence URL is upstream data. */
function safeHref(url: string | null | undefined): string | null {
    if (!url) return null;
    return /^https?:\/\//i.test(url) ? url : null;
}

/**
 * Click-popup HTML for one polygon feature. Every interpolated value is
 * escaped: these strings come from government feeds (holder names, unit
 * descriptions) and reach the DOM through Popup.setHTML.
 */
export function polygonPopupHtml(
    props: PolygonFeatureProperties,
    sources: Record<string, SourceAttribution> | undefined,
): string {
    const layerLabel = POLYGON_LAYER_LABELS[props.layer] ?? props.layer;
    const rows = (POLYGON_FIELDS[props.layer] ?? [])
        .filter(([key]) => props[key] !== null && props[key] !== undefined && props[key] !== '')
        .map(
            ([key, label]) =>
                `<div><span style="color:#9ca3af;">${escapeHtml(label)}:</span> ${escapeHtml(String(props[key]))}</div>`,
        )
        .join('');

    const src = sources?.[props.source_id];
    const href = safeHref(src?.license_url);
    const licence = src?.license_summary
        ? href
            ? `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer" style="color:#93c5fd;">${escapeHtml(src.license_summary)}</a>`
            : escapeHtml(src.license_summary)
        : 'licence unknown';

    return `<div style="font: 11px monospace; color: #e5e7eb; max-width: 280px;">
        <div style="font-weight: 600;">${escapeHtml(props.label ?? layerLabel)}</div>
        <div style="color: #9ca3af; margin-bottom: 4px;">${escapeHtml(layerLabel)} · ${escapeHtml(props.jurisdiction_code)}</div>
        ${rows}
        <div style="color: #9ca3af; margin-top: 4px;">Source: ${escapeHtml(src?.name ?? props.source_id)}</div>
        <div style="color: #9ca3af;">${licence}</div>
    </div>`;
}

/** One-line status for a requested layer, for the legend. */
export function polygonLayerStatus(meta: PolygonLayerMeta | undefined): string | null {
    if (!meta) return null;
    if (meta.mode === 'min_zoom') return `zoom in (≥ ${meta.min_zoom})`;
    if (meta.total_in_view === 0) return 'none in view';
    if (meta.truncated && meta.total_in_view !== null) {
        return `${meta.returned.toLocaleString()} of ${meta.total_in_view.toLocaleString()} — zoom in for all`;
    }
    return `${(meta.total_in_view ?? meta.returned).toLocaleString()} in view`;
}
