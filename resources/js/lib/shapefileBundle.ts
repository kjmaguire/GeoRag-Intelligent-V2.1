/**
 * Group a multi-file GIS dataset and its sidecars into one ZIP before upload.
 *
 * A shapefile is never one file. The geometry lives in `.shp`, the record
 * index in `.shx`, the attribute table in `.dbf`, and the coordinate system
 * in `.prj`; GDAL/pyogrio opens the `.shp` and reads the rest from beside it.
 * Upload the `.shp` on its own and the parse fails with
 *
 *     Unable to open <name>.shx ... Set SHAPE_RESTORE_SHX config option to YES
 *
 * which is what happened to every `.shp` in a folder import: the sidecars
 * have no upload category of their own, so the picker dropped them as
 * "unsupported" and sent the `.shp` alone.
 *
 * The server already handles the zipped form correctly - that is how
 * shapefiles are actually delivered - so the fix is to assemble that shape
 * here rather than to teach the backend to reassemble scattered parts it may
 * never receive.
 *
 * MapInfo works the same way and is handled here for the same reason: a
 * `.tab` is a short text header whose geometry lives in `.map`, attributes in
 * `.dat` and index in `.id`, and a `.mif` carries its attributes in a
 * separate `.mid`. Uploading a lone `.tab` is uploading nothing.
 *
 * Two rules this module will not bend:
 *
 *   1. Nothing is DISCARDED silently. A member that cannot be bundled comes
 *      back in `unusable` carrying the reason, and every incomplete set comes
 *      back with a `verdict` the screen renders. Real deliveries are messy -
 *      one folder here holds seven `.DAT` files, three `.MAP` files, an `.ID`
 *      and an `.IND` whose masters were never sent - and a bundler that
 *      answers "no master, therefore drop" loses all of it without a word.
 *   2. Member names stay BARE (`m.name`). An absolute or `..`-containing
 *      entry name is refused at the API edge as `unusable_archive`.
 */
import JSZip from 'jszip';

/**
 * Companion extensions that belong to a `.shp`.
 *
 * `.shx`, `.dbf` and `.prj` are the ones that matter; the rest are indexes
 * and encoding hints that GDAL uses when present. Including them is free and
 * dropping a `.cpg` silently mangles non-ASCII attribute values.
 */
export const SHAPEFILE_SIDECAR_EXTS = [
    'shx', 'dbf', 'prj', 'cpg', 'qpj',
    'sbn', 'sbx', 'qix', 'fbn', 'fbx',
    'ain', 'aih', 'atx', 'ixs', 'mxs',
] as const;

/**
 * MapInfo files GDAL will actually open.
 *
 * Only these two. A `.mid` opens on its own as well - measured - which is
 * exactly why it is a sidecar here and not a master: treat it as an entry
 * point and a MIF/MID pair gets ingested twice, once through the `.mif` and
 * once through the `.mid`.
 */
export const MAPINFO_MASTER_EXTS = ['tab', 'mif'] as const;

/**
 * Sidecars per MapInfo master, and which of them the master cannot open
 * without.
 *
 * A NATIVE `.tab` header carries no CoordSys at all - the coordinate system
 * lives in the `.map` - so a TAB delivered without its `.map` is a data loss
 * AND a CRS loss, and the resulting upload cannot be rescued by an EPSG
 * override either. `.ind` is an optional attribute index.
 *
 * None of these extensions gets an upload category of its own. `.dat` is
 * already claimed by the retired `xyz` category, and `.id`/`.map`/`.ind` are
 * three-letter names with far too many other meanings to route on.
 */
const MAPINFO_SIDECARS: Record<string, { all: string[]; required: string[] }> = {
    tab: { all: ['dat', 'map', 'id', 'ind'], required: ['dat', 'map', 'id'] },
    mif: { all: ['mid'], required: ['mid'] },
};

const SIDECARS = new Set<string>(SHAPEFILE_SIDECAR_EXTS);
const MASTERS = new Set<string>(MAPINFO_MASTER_EXTS);
const MAPINFO_SIDECAR_SET = new Set<string>(
    Object.values(MAPINFO_SIDECARS).flatMap((s) => s.all),
);

/**
 * Every extension that only ever travels as part of a bundle.
 *
 * This is what an `accept=` attribute needs on top of the category map. None
 * of these has an upload category - that is the point of them - so a picker
 * built from `acceptedExtensions()` alone greys them out in the OS dialog and
 * hands this module a lone `.shp`, manufacturing the very `.prj`-less bundle
 * whose missing CRS the ingest now refuses.
 */
export const BUNDLE_MEMBER_EXTS: string[] = [
    ...SHAPEFILE_SIDECAR_EXTS,
    ...MAPINFO_SIDECAR_SET,
].sort();

export type BundleKind = 'shapefile' | 'mapinfo';

export interface SpatialBundle {
    /** `<basename>.zip` - what gets uploaded, under the `spatial` category. */
    file: File;
    /** Base name of the dataset, for UI messages. */
    stem: string;
    /** Which family this is, for UI messages. */
    kind: BundleKind;
    /** Member file names in the ZIP, master first. */
    members: string[];
    /** Sidecars GDAL wants that were not in the selection. */
    missing: string[];
    /**
     * Plain-language verdict when the set is not openable as delivered, or
     * null when it is fine. Rendered next to the row: the point of bundling
     * is that the user finds out here, not from a failed run later.
     */
    verdict: string | null;
}

/** Backwards-compatible alias: every bundle used to be a shapefile. */
export type ShapefileBundle = SpatialBundle;

export interface UnusableFile {
    /** Kept, not dropped - a caller may still want to name or re-queue it. */
    file: File;
    /** Why it cannot be uploaded on its own. Render this. */
    reason: string;
}

export interface GroupResult {
    /** Zipped datasets, ready to queue as `spatial`. */
    bundles: SpatialBundle[];
    /** Everything that is its own upload - handle as before. */
    passthrough: File[];
    /**
     * Members whose master was not in the selection.
     *
     * NOT a graveyard: each carries the reason, and the screens say so. A
     * standalone `.dbf` is deliberately absent from this list - it is an
     * attribute table in its own right and goes to `passthrough`.
     */
    unusable: UnusableFile[];
}

function extOf(name: string): string {
    return name.split('.').pop()?.toLowerCase() ?? '';
}

function stemOf(f: File): string {
    return f.name.slice(0, f.name.length - extOf(f.name).length - 1);
}

/**
 * Key a file to its dataset group: directory path + base name, both
 * lower-cased.
 *
 * The directory matters. A folder import can contain `geology/faults.shp` and
 * `claims/faults.shp`, which are different layers that must not be zipped
 * into one bundle. `webkitRelativePath` is set by a directory picker and
 * empty for a plain multi-file selection, where a flat namespace is correct.
 *
 * Lower-casing the stem is not cosmetic. GDAL on Linux is case-sensitive
 * about sidecars, and real deliveries contain `drobeck_shumagin_veins.shp`
 * beside `Drobeck_Shumagin_Veins.prj`. Grouping case-insensitively is what
 * gets that `.prj` - and therefore that file's coordinate system - into the
 * ZIP at all.
 */
function groupKey(f: File): string {
    const rel = (f as File & { webkitRelativePath?: string }).webkitRelativePath || '';
    const dir = rel.includes('/') ? rel.slice(0, rel.lastIndexOf('/')) : '';
    return `${dir} ${stemOf(f).toLowerCase()}`;
}

async function zipOf(stem: string, members: File[]): Promise<File> {
    const zip = new JSZip();
    for (const m of members) {
        // Bare names only. rejectArchive() refuses an absolute or
        // `..`-containing entry name as `unusable_archive` before the
        // archive is ever opened.
        zip.file(m.name, m);
    }
    const blob = await zip.generateAsync({ type: 'blob', compression: 'DEFLATE' });
    return new File([blob], `${stem}.zip`, { type: 'application/zip' });
}

function shapefileVerdict(missing: string[]): string | null {
    // A missing .shx is not worth a word: GDAL rebuilds the index from the
    // .shp itself (SHAPE_RESTORE_SHX), measured on a real four-point file.
    const notes: string[] = [];
    if (missing.includes('prj')) {
        notes.push(
            'no .prj, so this file declares no coordinate system - set an EPSG code below, or the ingest will refuse it',
        );
    }
    if (missing.includes('dbf')) {
        notes.push('no .dbf, so the features will land with no attributes');
    }
    return notes.length > 0 ? `Incomplete shapefile: ${notes.join('; ')}.` : null;
}

function mapinfoVerdict(masterExt: string, missing: string[]): string | null {
    const absent = MAPINFO_SIDECARS[masterExt].required.filter((e) => missing.includes(e));
    if (absent.length === 0) return null;
    const list = absent.map((e) => `.${e}`).join(', ');
    if (masterExt === 'tab') {
        return (
            `Incomplete MapInfo TAB set: no ${list}. A .tab holds neither the geometry nor the ` +
            'coordinate system - both live in the .map - so GDAL cannot open this one. Add the ' +
            'missing files and drop the folder again.'
        );
    }
    return (
        `Incomplete MapInfo MIF set: no ${list}. The .mif still parses, but every attribute ` +
        'comes back empty because the values live in the .mid.'
    );
}

/**
 * Split a selection into spatial bundles and everything else.
 *
 * Only groups that actually contain a master (`.shp`, `.tab`, `.mif`) become
 * bundles. A `.dbf` sitting next to a `.csv` of the same name is left in
 * `passthrough` - it is only a sidecar if there is a `.shp` for it to be
 * beside, and on its own it is an attribute table the tabular ingest reads.
 *
 * The name is historical: it groups MapInfo too.
 */
export async function groupShapefiles(files: File[]): Promise<GroupResult> {
    const byKey = new Map<string, File[]>();
    for (const f of files) {
        const key = groupKey(f);
        const bucket = byKey.get(key);
        if (bucket) bucket.push(f);
        else byKey.set(key, [f]);
    }

    const bundles: SpatialBundle[] = [];
    const passthrough: File[] = [];
    const unusable: UnusableFile[] = [];

    for (const group of byKey.values()) {
        // Every file starts unclaimed; each master takes its own sidecars and
        // no sidecar is claimed twice, so a folder holding both `x.shp` and
        // `x.tab` produces two bundles rather than one bundle plus a lone
        // `.tab` that GDAL cannot open.
        const unclaimed = new Set<File>(group);

        const shp = group.find((f) => extOf(f.name) === 'shp');
        if (shp) {
            const members = [shp, ...group.filter((f) => SIDECARS.has(extOf(f.name)))];
            for (const m of members) unclaimed.delete(m);
            const present = new Set(members.map((m) => extOf(m.name)));
            // Only the three GDAL genuinely needs are worth warning about.
            const missing = ['shx', 'dbf', 'prj'].filter((e) => !present.has(e));
            const stem = stemOf(shp);
            bundles.push({
                file: await zipOf(stem, members),
                stem,
                kind: 'shapefile',
                members: members.map((m) => m.name),
                missing,
                verdict: shapefileVerdict(missing),
            });
        }

        for (const master of group.filter((f) => MASTERS.has(extOf(f.name)))) {
            if (!unclaimed.has(master)) continue;
            const masterExt = extOf(master.name);
            const wanted = MAPINFO_SIDECARS[masterExt].all;
            const members = [
                master,
                ...group.filter((f) => unclaimed.has(f) && wanted.includes(extOf(f.name))),
            ];
            for (const m of members) unclaimed.delete(m);
            const present = new Set(members.map((m) => extOf(m.name)));
            const missing = wanted.filter((e) => !present.has(e));
            const stem = stemOf(master);
            bundles.push({
                file: await zipOf(stem, members),
                stem,
                kind: 'mapinfo',
                members: members.map((m) => m.name),
                missing,
                verdict: mapinfoVerdict(masterExt, missing),
            });
        }

        for (const f of group) {
            if (!unclaimed.has(f)) continue;
            const ext = extOf(f.name);
            // A `.dbf` with no `.shp` beside it is NOT an orphan. It is a
            // dBASE attribute table, which is how a large part of the GIS
            // world still exports collar and sample tables, and pyogrio
            // reads one directly through the ESRI Shapefile driver. Dropping
            // it here is why a delivery of attribute tables could not be
            // imported at all.
            if (ext === 'dbf') {
                passthrough.push(f);
            } else if (SIDECARS.has(ext)) {
                unusable.push({
                    file: f,
                    reason:
                        `.${ext} belongs to a shapefile, and no ${stemOf(f)}.shp was selected - ` +
                        'there is nothing for it to attach to.',
                });
            } else if (MAPINFO_SIDECAR_SET.has(ext)) {
                unusable.push({
                    file: f,
                    reason:
                        `.${ext} belongs to a MapInfo table, and no ${stemOf(f)}.tab or ` +
                        `${stemOf(f)}.mif was selected - there is nothing for it to attach to.`,
                });
            } else {
                passthrough.push(f);
            }
        }
    }

    return { bundles, passthrough, unusable };
}

/** True if `ext` is a shapefile sidecar - used to explain why one was skipped. */
export function isShapefileSidecar(ext: string): boolean {
    return SIDECARS.has(ext.toLowerCase());
}

/** True if `ext` is a MapInfo sidecar - never an upload on its own. */
export function isMapInfoSidecar(ext: string): boolean {
    return MAPINFO_SIDECAR_SET.has(ext.toLowerCase());
}
