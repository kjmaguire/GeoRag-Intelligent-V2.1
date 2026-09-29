/**
 * Pure decisions behind Foundry/Workspace, kept out of the 1,400-line page so
 * they can be tested (2026-09-29 audit: FE-11, FE-18, FE-25).
 */

/** The deferred `viz3d` group WorkspaceController sends after first paint. */
export const VIZ3D_PROPS = [
    'first_holes_intervals',
    'surveys_3d',
    'structures_3d',
    'assay_composites_3d',
    'assay_elements_3d',
    'significant_intersections_3d',
    'structures_visual_3d',
    'commodity_samples_3d',
    'commodity_keys_3d',
    'survey_holes_downsampled',
] as const;

/** Props the LOGS panel reloads when the hole or its curve selection changes. */
export const LOG_PROPS = [
    'log_tracks',
    'log_available_curves',
    'log_selected_curves',
    'log_hole_id',
    'log_depth_max',
    'log_hole_total_depth',
    'log_hole_easting',
    'log_hole_northing',
    'log_lithology_intervals',
    'log_alteration_intervals',
    'log_mineralization_intervals',
    'log_tracks_truncated',
    'log_hole_options',
] as const;

export interface ReloadPlan {
    /** 'all' = a full reload; otherwise the eager props to reload. Empty = nothing. */
    props: 'all' | string[];
    /** Whether the deferred 3D group is out of date. */
    viz3d: boolean;
}

/**
 * What a `workspace.data_updated` broadcast should reload.
 *
 * The page used to `router.reload()` everything — including the multi-MB 3D
 * payload — on any event carrying `collars` OR `reports`, and every ingest
 * carries `reports` (DebounceWorkspaceMvRefresh emits it as a superset). Now:
 *
 *   collars / assays → everything (every panel is keyed on the collar set);
 *   structures       → map layer counts + extent + structure counts, and 3D;
 *   curves           → the LOGS panel + curve summary, and 3D (intervals are
 *                      derived from curves);
 *   anything else    → nothing this page shows (reports, quality, …).
 *
 * The caller decides WHEN to fetch the 3D group: immediately if a 3D-using
 * mode is on screen, otherwise on the next visit to one.
 */
export function reloadPlan(affectedTypes: readonly string[]): ReloadPlan {
    const t = new Set(affectedTypes);
    if (t.has('collars') || t.has('assays')) {
        return { props: 'all', viz3d: true };
    }
    const props = new Set<string>();
    let viz3d = false;
    if (t.has('structures')) {
        for (const p of ['project', 'project_layers', 'project_extent', 'structures_count', 'structures_visual_count']) props.add(p);
        viz3d = true;
    }
    if (t.has('curves')) {
        for (const p of [...LOG_PROPS, 'project', 'curve_summary', 'well_log_curves_count', 'intervals_count']) props.add(p);
        viz3d = true;
    }
    return { props: Array.from(props), viz3d };
}

export type View3DName =
    | 'lithology'
    | 'trajectories'
    | 'spiral'
    | 'stereosphere'
    | 'project_stereonet'
    | 'assay_grade'
    | 'significant_intersections'
    | 'structure_discs'
    | 'commodity_samples';

/**
 * The 3D sub-view the mode opens on: one that HAS data, judged by the eager
 * counts (the 3D arrays themselves arrive deferred).
 *
 * FE-25: it used to open on Stereosphere when only
 * gold.structure_measurements_visual had rows — but Stereosphere draws
 * silver.structure, so the user landed on an empty state claiming there were
 * no measurements. Structure Discs is the view that draws the gold set.
 */
export function initialView3D(c: {
    intervalsCount: number;
    collarsCount: number;
    structuresCount: number;
    structuresVisualCount: number;
}): View3DName {
    if (c.intervalsCount > 0) return 'lithology';
    if (c.collarsCount > 0) return 'trajectories';
    if (c.structuresCount > 0) return 'stereosphere';
    if (c.structuresVisualCount > 0) return 'structure_discs';
    return 'lithology';
}

/**
 * Label for a hole's easting/northing. It said "UTM 13N" for every project
 * (FE-18); it now names the project's declared CRS, or says there is none.
 */
export function crsLabel(epsg: number | null | undefined): string {
    return epsg ? `EPSG:${epsg}` : 'CRS not declared';
}

/**
 * Copilot quick prompts, derived from the project's commodity.
 *
 * FE-25: these were three uranium prompts (U₃O₈ grades, a Smith
 * Ranch-Highland analogue) shown on every project whatever it explores for.
 */
export function copilotQuickPrompts(commodity: string | null | undefined): string[] {
    const c = commodity?.trim();
    return [
        'Summarise the mineralised zones in this project',
        c ? `Which holes have the best ${c} intervals?` : 'Which holes have the best-grade intervals?',
        c ? `Which reports describe analogue ${c} deposits for this project?` : 'Which reports describe analogue deposits for this project?',
    ];
}
