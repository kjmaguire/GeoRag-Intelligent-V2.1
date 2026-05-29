/**
 * Public Geoscience — frontend contract types.
 *
 * Matches the shape emitted by Laravel's
 *   App\Http\Resources\PublicGeoscience\JurisdictionResource
 *   App\Http\Controllers\Api\V1\PublicGeoscience\JurisdictionController
 *
 * The outer response envelope reuses the dashboard `DashboardResponse<T>`
 * shape so the existing `useDashboardFetch` hook can power this surface
 * without a parallel hook.
 */

export type JurisdictionStatus = 'active' | 'coming_soon' | 'deprecated';

export type JurisdictionLevel =
    | 'country'
    | 'province'
    | 'territory'
    | 'state'
    | 'federal';

export type CanonicalType =
    | 'mine'
    | 'mineral_occurrence'
    | 'drillhole_collar'
    | 'resource_potential_zone';

export interface PublicGeoSource {
    source_id: string;
    name: string;
    canonical_type: CanonicalType;
    service_url: string;
    layer_index: number | null;
    source_crs: number | null;
    license_summary: string | null;
    license_url: string | null;
    refresh_cadence: string | null;
    last_refreshed_at: string | null;
}

/**
 * A GeoJSON Polygon (or MultiPolygon in later phases) — directly consumable
 * by MapLibre `fitBounds` after extracting its envelope.
 */
export interface BboxGeoJson {
    type: 'Polygon' | 'MultiPolygon';
    coordinates: number[][][] | number[][][][];
}

export interface Jurisdiction {
    jurisdiction_code: string;
    country_code: string;
    display_name: string;
    level: JurisdictionLevel;
    status: JurisdictionStatus;
    primary_authority: string | null;
    license_summary: string | null;
    license_url: string | null;
    default_source_crs: number | null;
    refresh_cadence: string | null;
    last_refreshed_at: string | null;
    teaser: string | null;
    sort_order: number;
    bbox: BboxGeoJson | null;
    sources: PublicGeoSource[];
}

export interface CountryGroup {
    country_code: string;
    display_name: string;
    jurisdictions: Jurisdiction[];
}

export interface JurisdictionsPayload {
    countries: CountryGroup[];
    counts: {
        total: number;
        active: number;
        coming_soon: number;
    };
}
