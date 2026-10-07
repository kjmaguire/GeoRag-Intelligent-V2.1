/**
 * Payload-limit helpers for the project workspace.
 *
 * WorkspaceController bounds how many holes / stations it sends and reports
 * which bounds actually bit in the `truncation` prop. These helpers turn that
 * into human copy and keep the LOGS curve-toggle rules in one testable place.
 */

export interface CountLimit {
    shown: number;
    total: number;
    truncated: boolean;
}

export interface WorkspaceTruncation {
    collars: CountLimit;
    interval_holes: CountLimit;
    /** Sent in the deferred 3D group since FE-11; absent until it loads. */
    survey_holes_downsampled?: number;
}

const fmt = (n: number): string => n.toLocaleString('en-US');

/** Terrain model behind `silver.collars.elevation_dem_source`, and the credit its licence requires. */
const COPERNICUS_GLO30 =
    'Copernicus DEM GLO-30 (30 m), produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved';

interface CollarElevationSource {
    elevation_source?: 'file' | 'terrain' | null;
    elevation_dem_source?: string | null;
}

/**
 * Notice for holes drawn at a terrain-model height because their file had no
 * elevation, naming the model that was actually used (and crediting it where
 * its licence asks). Null when no hole is. Independent of the truncation prop:
 * it must show even when that prop is absent.
 */
export function describeTerrainElevation(collars: readonly CollarElevationSource[]): string | null {
    const terrain = collars.filter((c) => c.elevation_source === 'terrain');
    if (terrain.length === 0) return null;
    const sources = new Set(terrain.map((c) => c.elevation_dem_source ?? ''));
    const model = Array.from(sources)
        .map((s) => (s === 'copernicus_glo30' ? COPERNICUS_GLO30 : s ? `terrain model "${s}"` : 'a terrain model'))
        .join('; ');
    const n = terrain.length;
    return `${fmt(n)} ${n === 1 ? 'hole has' : 'holes have'} no elevation in the file and ${n === 1 ? 'is' : 'are'} drawn at ground height from ${model}. A surface model: under forest it reads the canopy, not bare ground.`;
}

/**
 * Notices to show when the server capped what it sent. Empty when nothing was
 * truncated (or when the prop is missing, e.g. an older cached payload).
 */
export function describeTruncation(t: WorkspaceTruncation | null | undefined): string[] {
    if (!t) return [];
    const notices: string[] = [];
    if (t.collars?.truncated) {
        notices.push(`Showing ${fmt(t.collars.shown)} of ${fmt(t.collars.total)} holes on the map and in 3D.`);
    }
    if (t.interval_holes?.truncated && (!t.collars?.truncated || t.interval_holes.shown < t.collars.shown)) {
        notices.push(`3D lithology shows ${fmt(t.interval_holes.shown)} of ${fmt(t.interval_holes.total)} holes.`);
    }
    if ((t.survey_holes_downsampled ?? 0) > 0) {
        notices.push(
            `Survey stations thinned for ${fmt(t.survey_holes_downsampled ?? 0)} holes (first and last kept).`,
        );
    }
    return notices;
}

/**
 * Next curve selection after the user clicks `name`.
 *
 * Turning a curve off is refused when it is the last one (an empty selection
 * would make the server fall back to its default and look like the click did
 * nothing); turning one on is refused at `max` so the payload stays bounded.
 * Returns null when the click is a no-op.
 */
export function toggleCurveSelection(selected: readonly string[], name: string, max: number): string[] | null {
    if (selected.includes(name)) {
        if (selected.length <= 1) return null;
        return selected.filter((n) => n !== name);
    }
    if (selected.length >= max) return null;
    return [...selected, name];
}
