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
    /**
     * Holes whose file had no elevation, drawn at the terrain model's ground
     * height (silver.collars.elevation_dem_m) instead of z = 0.
     */
    terrain_elevation_holes?: number;
}

const fmt = (n: number): string => n.toLocaleString('en-US');

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
    if ((t.terrain_elevation_holes ?? 0) > 0) {
        const n = t.terrain_elevation_holes ?? 0;
        notices.push(
            `${fmt(n)} ${n === 1 ? 'hole has' : 'holes have'} no elevation in the file; drawn at terrain-model ground height (Copernicus 30 m).`,
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
