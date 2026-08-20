/**
 * Group a shapefile and its sidecars into one ZIP before upload.
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

const SIDECARS = new Set<string>(SHAPEFILE_SIDECAR_EXTS);

export interface ShapefileBundle {
    /** `<basename>.zip` - what gets uploaded, under the `spatial` category. */
    file: File;
    /** Base name of the shapefile, for UI messages. */
    stem: string;
    /** Member file names in the ZIP, `.shp` first. */
    members: string[];
    /** Sidecars GDAL wants that were not in the selection. */
    missing: string[];
}

export interface GroupResult {
    /** Zipped shapefiles, ready to queue as `spatial`. */
    bundles: ShapefileBundle[];
    /** Everything that is not part of a shapefile - handle as before. */
    passthrough: File[];
    /** Sidecars with no `.shp` alongside them. Nothing can be done with these. */
    orphanSidecars: File[];
}

function extOf(name: string): string {
    return name.split('.').pop()?.toLowerCase() ?? '';
}

function stemOf(f: File): string {
    return f.name.slice(0, f.name.length - extOf(f.name).length - 1);
}

/**
 * Key a file to its shapefile group: directory path + base name, both
 * lower-cased.
 *
 * The directory matters. A folder import can contain `geology/faults.shp` and
 * `claims/faults.shp`, which are different layers that must not be zipped
 * into one bundle. `webkitRelativePath` is set by a directory picker and
 * empty for a plain multi-file selection, where a flat namespace is correct.
 */
function groupKey(f: File): string {
    const rel = (f as File & { webkitRelativePath?: string }).webkitRelativePath || '';
    const dir = rel.includes('/') ? rel.slice(0, rel.lastIndexOf('/')) : '';
    return `${dir} ${stemOf(f).toLowerCase()}`;
}

/**
 * Split a selection into shapefile bundles and everything else.
 *
 * Only groups that actually contain a `.shp` become bundles. A `.dbf` sitting
 * next to a `.csv` of the same name is left in `passthrough` - it is only a
 * sidecar if there is something for it to be beside.
 */
export async function groupShapefiles(files: File[]): Promise<GroupResult> {
    const byKey = new Map<string, File[]>();
    for (const f of files) {
        const key = groupKey(f);
        const bucket = byKey.get(key);
        if (bucket) bucket.push(f);
        else byKey.set(key, [f]);
    }

    const bundles: ShapefileBundle[] = [];
    const passthrough: File[] = [];
    const orphanSidecars: File[] = [];

    for (const group of byKey.values()) {
        const shp = group.find((f) => extOf(f.name) === 'shp');
        if (!shp) {
            for (const f of group) {
                if (SIDECARS.has(extOf(f.name))) orphanSidecars.push(f);
                else passthrough.push(f);
            }
            continue;
        }

        const members = [shp, ...group.filter((f) => SIDECARS.has(extOf(f.name)))];
        // A same-stem file that is neither the .shp nor a sidecar (say
        // faults.pdf) is its own upload, not part of the bundle.
        for (const f of group) {
            if (f !== shp && !SIDECARS.has(extOf(f.name))) passthrough.push(f);
        }

        const zip = new JSZip();
        for (const m of members) {
            zip.file(m.name, m);
        }
        const blob = await zip.generateAsync({ type: 'blob', compression: 'DEFLATE' });
        const stem = stemOf(shp);
        const present = new Set(members.map((m) => extOf(m.name)));
        bundles.push({
            file: new File([blob], `${stem}.zip`, { type: 'application/zip' }),
            stem,
            members: members.map((m) => m.name),
            // Only the three GDAL genuinely needs are worth warning about.
            missing: ['shx', 'dbf', 'prj'].filter((e) => !present.has(e)),
        });
    }

    return { bundles, passthrough, orphanSidecars };
}

/** True if `ext` is a shapefile sidecar - used to explain why one was skipped. */
export function isShapefileSidecar(ext: string): boolean {
    return SIDECARS.has(ext.toLowerCase());
}
