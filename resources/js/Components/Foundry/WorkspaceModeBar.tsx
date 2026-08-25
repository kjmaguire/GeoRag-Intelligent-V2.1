import { router } from '@inertiajs/react';
import { Segmented } from './primitives';

/**
 * The Workspace mode selector, shared by every page that IS a Workspace mode.
 *
 * Workspace renders six modes inside one page (`?mode=`), and Rasters is a
 * seventh that lives at its own URL. Both render THIS bar, so a geologist
 * sees one row of modes with the current one lit regardless of which of the
 * two pages is actually mounted.
 *
 * Why Rasters is a URL rather than a seventh in-page panel: the raster
 * catalogue has its own controller, its own RLS-scoped query and a feature
 * test suite pinning `/projects/{slug}/rasters` to the `Foundry/RasterLayers`
 * component. Re-rendering the same catalogue inside Workspace would put a
 * second copy of that page in the codebase and leave the two to drift — the
 * exact failure `lib/uploadCategories.ts` was written to end. A shared bar
 * plus a real URL gives the same information architecture with one renderer,
 * and every mode stays deep-linkable and back-button-correct.
 *
 * `mode` is deliberately a plain string rather than Workspace's `Mode` union:
 * this module is imported BY Workspace, so importing the union back out of it
 * would be circular.
 */
export type WorkspaceModeId =
    | 'map'
    | 'rasters'
    | 'section'
    | '3d'
    | 'structure'
    | 'logs'
    | 'compare';

/**
 * Modes in display order.
 *
 * Rasters sits second, directly beside Map (2026-08-25). A raster's footprint
 * is a map layer — the catalogue tells you what ground each scan covers — so
 * two nav entries away from the map it overlays was the wrong place for it.
 *
 * `page` marks the modes that are NOT panels inside Workspace. Everything
 * without one is switched in place.
 */
export const WORKSPACE_MODES: ReadonlyArray<{
    value: WorkspaceModeId;
    label: string;
    /** URL suffix, for modes that live on their own page. */
    page?: string;
}> = [
    { value: 'map', label: 'Map' },
    { value: 'rasters', label: 'Rasters', page: '/rasters' },
    { value: 'section', label: 'Section' },
    { value: '3d', label: '3D' },
    { value: 'structure', label: 'Structure' },
    { value: 'logs', label: 'Logs' },
    { value: 'compare', label: 'Compare' },
];

/** Modes Workspace itself renders as panels. */
export const IN_PAGE_MODES = WORKSPACE_MODES.filter((m) => m.page === undefined).map(
    (m) => m.value,
);

export default function WorkspaceModeBar({
    slug,
    active,
    onSelectInPage,
}: {
    slug: string;
    active: WorkspaceModeId;
    /**
     * Called for a mode Workspace renders itself. Omitted on the Rasters
     * page, which has no panels of its own — there, selecting an in-page
     * mode navigates back to Workspace carrying `?mode=`.
     */
    onSelectInPage?: (mode: WorkspaceModeId) => void;
}) {
    function select(next: WorkspaceModeId) {
        if (next === active) return;

        const target = WORKSPACE_MODES.find((m) => m.value === next);
        if (target?.page !== undefined) {
            router.visit(`/projects/${slug}${target.page}`);
            return;
        }
        if (onSelectInPage) {
            onSelectInPage(next);
            return;
        }
        // Arriving from a page-mode: hand Workspace the mode on the URL so
        // it opens on the right panel instead of defaulting to Map.
        router.visit(`/projects/${slug}/workspace?mode=${next}`);
    }

    return (
        <Segmented<WorkspaceModeId>
            value={active}
            onChange={select}
            options={WORKSPACE_MODES.map((m) => ({ value: m.value, label: m.label }))}
        />
    );
}
