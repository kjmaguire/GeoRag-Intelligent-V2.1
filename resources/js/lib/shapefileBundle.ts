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
 * Two things this module does that go past assembling a ZIP, both because the
 * alternative is asking the user for information already sitting in the folder
 * they just dropped:
 *
 *   - CRS DONATION. When the selection's coordinate-system sidecars - `.prj`
 *     and `.qpj` alike - agree on exactly ONE WKT, and that WKT actually
 *     names a CRS, those bytes are copied into every shapefile bundle that
 *     declares none of its own, under the recipient's own stem. Nothing is
 *     parsed here beyond lifting a display name out of the WKT: the server
 *     reads the copy with pyproj exactly as it reads any other `.prj`, and it
 *     still measures the fit of the coordinate system against the geometry
 *     and stores that score as `crs_confidence`/`georef_method`, so a donated
 *     CRS that does not match is not recorded as a confident one. Two
 *     distinct WKTs in one selection and nothing is donated at all - a
 *     delivery carrying two coordinate systems must not have one of them
 *     quietly spread across the other's files.
 *
 *     A bundle that took a copy carries `crsFrom`, and that is the ONLY way
 *     to identify a recipient. Stems are not unique across folders - see
 *     `groupKey`, which keeps `geology/faults.shp` and `claims/faults.shp`
 *     deliberately apart, and the real delivery has exactly that - so a
 *     screen matching recipients by stem mis-attributes the donation and
 *     strips the wrong member back out when the user declines it.
 *   - RASTER `.tab`. A MapInfo TAB whose header says `Type "RASTER"` is the
 *     georeferencing for a scanned image, not a vector table. It has no
 *     `.dat`/`.map`/`.id` and never will, so reporting it as an incomplete
 *     vector set asks the user for files that do not exist.
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

/**
 * Where a bundle's coordinate system came from, when it came from another
 * file.
 *
 * Carried ON the bundle rather than looked up by stem in `CrsDonation`,
 * because stems are not unique: a folder import holding `geology/faults.shp`
 * and `claims/faults.shp` produces two bundles with the same `stem`, only one
 * of which may have taken a copy. Matching on the stem attributes the
 * donation to both, and the "remove the donated .prj" path then strips a
 * member out of an archive that never received one.
 *
 * `memberName` is the exact ZIP entry that was added, so removing it again is
 * an exact-name operation and not a reconstruction from the stem.
 */
export interface CrsProvenance {
    /** File the WKT came from, e.g. `Drobeck_Shumagin_Veins.prj`. */
    sourceName: string;
    /** Human label parsed out of the WKT, e.g. `NAD 1983 UTM Zone 4N`. */
    label: string;
    /** The exact entry written into this bundle, e.g. `geology_poly.prj`. */
    memberName: string;
}

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
    /**
     * Set when this bundle was given a `.prj` it did not have, null when its
     * coordinate system is its own. This is the recipient marker - never the
     * stem.
     */
    crsFrom?: CrsProvenance | null;
}

/** Backwards-compatible alias: every bundle used to be a shapefile. */
export type ShapefileBundle = SpatialBundle;

export interface UnusableFile {
    /** Kept, not dropped - a caller may still want to name or re-queue it. */
    file: File;
    /** Why it cannot be uploaded on its own. Render this. */
    reason: string;
}

/**
 * One `.prj` from the selection, copied into the bundles that had none.
 *
 * Only ever set when the whole selection agrees on a single coordinate
 * system, and only ever reported when at least one bundle actually took a
 * copy. The screens render this as one line with a control to turn it off:
 * applied by default because the point is to stop making the user type the
 * same EPSG code once per file, visible because a CRS the file did not
 * declare is not the same fact as a CRS it did.
 */
export interface CrsDonation {
    /** File name the WKT came from, e.g. `Drobeck_Shumagin_Veins.prj`. */
    sourceName: string;
    /** Raw `.prj` text, copied verbatim into each recipient bundle. */
    wkt: string;
    /** Human label parsed out of the WKT's PROJCS/GEOGCS name, for the UI. */
    label: string;
    /**
     * Stems of the bundles that received a copy, one entry per recipient.
     *
     * For the summary banner - how many bundles took it, and which datasets
     * to name in one line. NOT an identity: two shapefiles in different
     * folders can share a stem, so a screen deciding whether a given bundle
     * is a recipient must read `SpatialBundle.crsFrom` instead. Duplicate
     * entries here are real, not a bug: they are two different bundles.
     */
    appliedTo: string[];
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
    /**
     * The single coordinate system this selection agreed on, and where it was
     * copied to, or null when there was nothing to donate, nothing agreed on,
     * or nothing that needed it.
     */
    crsDonation: CrsDonation | null;
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

/**
 * @param donated Extra member written under a name of our choosing rather
 *   than its own - the donated `.prj`, which has to arrive as `<stem>.prj`
 *   for GDAL to read it as this shapefile's coordinate system. The source
 *   File is handed to JSZip directly so the bytes are copied verbatim, with
 *   no text round-trip to mangle the encoding of a WKT.
 */
async function zipOf(
    stem: string,
    members: File[],
    donated: { name: string; source: File } | null = null,
): Promise<File> {
    const zip = new JSZip();
    for (const m of members) {
        // Bare names only. rejectArchive() refuses an absolute or
        // `..`-containing entry name as `unusable_archive` before the
        // archive is ever opened.
        zip.file(m.name, m);
    }
    if (donated) {
        zip.file(donated.name, donated.source);
    }
    const blob = await zip.generateAsync({ type: 'blob', compression: 'DEFLATE' });
    return new File([blob], `${stem}.zip`, { type: 'application/zip' });
}

/**
 * Compare two `.prj` files by content, not by bytes.
 *
 * The same WKT is routinely written twice with a different trailing newline,
 * or wrapped across lines by one toolchain and not another. Treating those as
 * two coordinate systems would suppress the donation on exactly the deliveries
 * that need it most.
 */
function normaliseWkt(text: string): string {
    return text.trim().replace(/\s+/g, ' ');
}

/**
 * Extensions that carry a coordinate-system declaration beside a `.shp`.
 *
 * `.qpj` is here because QGIS wrote its own copy of the WKT into one for
 * years, and that copy can say something DIFFERENT from the `.prj` beside it
 * - or be the only declaration in the folder. Harvesting only `.prj` made a
 * shapefile carrying just a `.qpj` read as declaring nothing, which is the
 * one state that attracts a donation, so the file would be handed a
 * coordinate system contradicting the one it actually declares.
 */
const CRS_SIDECARS = new Set(['prj', 'qpj']);

/**
 * The WKT keywords that actually name a coordinate reference system: WKT1's
 * `PROJCS`/`GEOGCS` and WKT2's `PROJCRS`/`GEOGCRS`.
 *
 * This is the gate on donating at all. "The selection agrees on one WKT" is
 * satisfied by a single EMPTY `.prj`, and by one holding a stray line of
 * junk - a set of size one either way - and `crsLabel` then falls back to the
 * file name, so the screen would offer `donor.prj` as the coordinate system
 * being applied to seven other files. A donor has to name a CRS; anything
 * else and the recipients keep the ordinary "set an EPSG code" verdict.
 */
const CRS_WKT_RE = /\b(?:PROJCS|GEOGCS|PROJCRS|GEOGCRS)\s*\[/i;

/**
 * The one piece of WKT reading this module does: the quoted name on PROJCS,
 * or failing that on GEOGCS (and their WKT2 spellings), for a label a human
 * can check at a glance.
 *
 * Deliberately NOT a WKT -> EPSG resolution. That belongs to pyproj on the
 * server, which already does it for every other `.prj`; a second, weaker
 * implementation in the browser would be a new way to be confidently wrong
 * about a coordinate system, which is the bug this whole change exists to
 * close.
 */
function crsLabel(wkt: string, fallback: string): string {
    const match =
        /\bPROJCS\s*\[\s*"([^"]+)"/i.exec(wkt) ??
        /\bPROJCRS\s*\[\s*"([^"]+)"/i.exec(wkt) ??
        /\bGEOGCS\s*\[\s*"([^"]+)"/i.exec(wkt) ??
        /\bGEOGCRS\s*\[\s*"([^"]+)"/i.exec(wkt);
    if (!match) return fallback;
    return match[1].replace(/_/g, ' ').trim();
}

/**
 * A MapInfo TAB header declaring `Type "RASTER"` (NATIVE tables write it
 * unquoted, raster ones quoted - accept both).
 */
const RASTER_TAB_RE = /^\s*Type\s+"?RASTER"?/im;

/** `  File "bmgc_ungaissouth_geology_1990.tif"` in a TAB header. */
const TAB_IMAGE_RE = /^\s*File\s+"([^"]+)"/im;

/** Strip any directory part a TAB header wrote into its `File` line. */
function baseName(path: string): string {
    return path.split(/[\\/]/).pop() ?? path;
}

/**
 * @param hasQpj The set carries a `.qpj`. It is not CRS-less then, so the
 *   "declares no coordinate system" wording would be false - but the ingest
 *   inventories `.shx`/`.dbf`/`.prj`/`.cpg` and refuses on a missing `.prj`,
 *   so an EPSG code is still the way through.
 */
function shapefileVerdict(missing: string[], hasQpj = false): string | null {
    // A missing .shx is not worth a word: GDAL rebuilds the index from the
    // .shp itself (SHAPE_RESTORE_SHX), measured on a real four-point file.
    const notes: string[] = [];
    if (missing.includes('prj')) {
        notes.push(
            hasQpj
                ? 'no .prj - the coordinate system is declared in a .qpj, which the ingest does not read, so set an EPSG code below or add the .prj'
                : 'no .prj, so this file declares no coordinate system - set an EPSG code below, or the ingest will refuse it',
        );
    }
    if (missing.includes('dbf')) {
        notes.push('no .dbf, so the features will land with no attributes');
    }
    return notes.length > 0 ? `Incomplete shapefile: ${notes.join('; ')}.` : null;
}

/**
 * Verdict for a bundle that got its coordinate system from another file.
 *
 * It must not read as though the set were complete - it is not, the `.prj`
 * is a copy - and it must name the source, because a coordinate system the
 * file did not declare is the one thing on this screen the user is best
 * placed to catch.
 *
 * `missing` here has already had `prj` removed, so the shared builder never
 * contradicts this by also demanding an EPSG code.
 */
function donatedCrsVerdict(missing: string[], sourceName: string, label: string): string {
    const note =
        `No .prj of its own: the coordinate system was copied from ${sourceName} (${label}), ` +
        'the only one in this selection. The ingest still checks it against the geometry and ' +
        'flags it if it does not fit.';
    const rest = shapefileVerdict(missing);
    return rest ? `${rest} ${note}` : note;
}

/**
 * Why a raster TAB is not an incomplete vector set.
 *
 * The old message told the user to add the `.dat`, `.map` and `.id` - files a
 * raster TAB has never had. Advice that cannot be followed reads as a broken
 * importer, and it buried the two files carrying the georeferencing for the
 * scanned geology maps in the delivery.
 *
 * The message it was replaced with then promised its own impossible thing:
 * "upload the image itself and this file's coordinate system will be
 * applied." Nothing applies it. This TAB is reported as unusable and never
 * uploaded, and no code reads its `CoordSys` line, so the image goes up as an
 * ordinary image. Saying otherwise inside the feature built to stop giving
 * unfollowable advice is the same bug one message further on.
 *
 * The file name is deliberately NOT repeated here: both screens render an
 * unusable row as `<file name>: <reason>`, so a reason that opens with the
 * name prints it twice.
 */
function rasterTabReason(image: string | null, imageSelected: boolean): string {
    const head =
        image === null
            ? 'Georeferencing for a scanned image, not a vector table.'
            : `Georeferencing for ${image}, not a vector table.`;
    const tail =
        image === null
            ? ''
            : imageSelected
              ? ` ${image} is in this selection and is being uploaded on its own.`
              : ` ${image} was not selected - add it and drop the folder again to upload it.`;
    return `${head}${tail} The coordinate system in this file is not read yet.`;
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
    const bundles: SpatialBundle[] = [];
    const passthrough: File[] = [];
    const unusable: UnusableFile[] = [];
    const rasterTabs: UnusableFile[] = [];

    // ---- Raster TABs, before anything can mistake one for a vector set ----
    //
    // A TAB's header is plain text at the very start of the file, so telling
    // the two kinds apart - the extension is identical - costs one slice.
    //
    // Only the HEAD is read. `f.text()` reads the whole file, and a `.tab` is
    // only reliably short for the raster ones: a NATIVE table's header grows
    // with its field list, and nothing stops a caller dropping something much
    // larger with a `.tab` name. Both regexes below match inside the first
    // couple of hundred bytes on every real fixture (401-748 bytes end to
    // end), so 8 KiB is slack, not a limit anything real approaches.
    const selectedNames = new Set(files.map((f) => f.name.toLowerCase()));
    const vectorFiles: File[] = [];
    for (const f of files) {
        if (extOf(f.name) !== 'tab') {
            vectorFiles.push(f);
            continue;
        }
        const header = await f.slice(0, 8192).text();
        if (!RASTER_TAB_RE.test(header)) {
            vectorFiles.push(f);
            continue;
        }
        const match = TAB_IMAGE_RE.exec(header);
        const image = match ? baseName(match[1]) : null;
        rasterTabs.push({
            file: f,
            reason: rasterTabReason(
                image,
                image !== null && selectedNames.has(image.toLowerCase()),
            ),
        });
    }

    // ---- The one coordinate system this selection agrees on, if any ----
    //
    // Harvested across the WHOLE selection, not per group: the `.prj` that
    // rescues seven CRS-less shapefiles is routinely three folders away from
    // all of them, attached to the one dataset that was exported properly.
    // An orphaned `.prj` counts too - it is a declaration of the delivery's
    // coordinate system whoever it was meant for. So does a `.qpj`: leaving
    // it out of the comparison is how a folder holding one coordinate system
    // in `.prj` files and a second in a `.qpj` would look unanimous.
    const crsFiles: { file: File; text: string; key: string }[] = [];
    for (const f of vectorFiles) {
        if (!CRS_SIDECARS.has(extOf(f.name))) continue;
        const text = await f.text();
        crsFiles.push({ file: f, text, key: normaliseWkt(text) });
    }
    const distinctWkts = new Set(crsFiles.map((c) => c.key));
    // Prefer a `.prj` as the named source purely so the file the screen tells
    // the user to go and check is the one they will recognise. The bytes are
    // the same whichever is picked, or there would be more than one distinct
    // WKT here and no donation at all.
    const agreed =
        distinctWkts.size === 1
            ? (crsFiles.find((c) => extOf(c.file.name) === 'prj') ?? crsFiles[0])
            : null;
    // Agreement is not enough: one empty `.prj` agrees with itself. Donate
    // only text that names a CRS.
    const donor = agreed && CRS_WKT_RE.test(agreed.text) ? agreed : null;
    const donorLabel = donor ? crsLabel(donor.text, donor.file.name) : '';
    const appliedTo: string[] = [];

    const byKey = new Map<string, File[]>();
    for (const f of vectorFiles) {
        const key = groupKey(f);
        const bucket = byKey.get(key);
        if (bucket) bucket.push(f);
        else byKey.set(key, [f]);
    }

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
            // A `.qpj` is a declaration of this set's own coordinate system.
            // It does not satisfy the ingest, which reads the `.prj` - so it
            // stays in `missing` - but it does disqualify the set as a
            // recipient: copying another file's WKT in beside a `.qpj` saying
            // something else is the contradiction this feature must not
            // create.
            const declaresCrs = present.has('prj') || present.has('qpj');

            // The copy is named for its recipient, not for its source: GDAL
            // reads `<stem>.prj` beside `<stem>.shp` and nothing else.
            const donated =
                donor && !declaresCrs ? { name: `${stem}.prj`, source: donor.file } : null;
            const reported = donated ? missing.filter((e) => e !== 'prj') : missing;
            if (donated) appliedTo.push(stem);

            bundles.push({
                file: await zipOf(stem, members, donated),
                stem,
                kind: 'shapefile',
                members: donated
                    ? [...members.map((m) => m.name), donated.name]
                    : members.map((m) => m.name),
                missing: reported,
                // The recipient marker. Two bundles can share `stem`; only
                // the one that actually took a copy carries this.
                crsFrom:
                    donated && donor
                        ? {
                              sourceName: donor.file.name,
                              label: donorLabel,
                              memberName: donated.name,
                          }
                        : null,
                verdict:
                    donated && donor
                        ? donatedCrsVerdict(reported, donor.file.name, donorLabel)
                        : shapefileVerdict(missing, present.has('qpj')),
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
                // A TAB's coordinate system lives in its .map; GDAL never
                // reads a .prj beside one, so a MapInfo bundle is never a
                // recipient.
                crsFrom: null,
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

    // A donation only exists once something took it. One orphaned `.prj` and
    // nothing else in the selection is still an orphaned `.prj`, and saying
    // otherwise would put a "coordinate system applied" line on a screen where
    // nothing was applied to anything.
    const crsDonation: CrsDonation | null =
        donor && appliedTo.length > 0
            ? {
                  sourceName: donor.file.name,
                  wkt: donor.text,
                  label: donorLabel,
                  appliedTo,
              }
            : null;

    // An orphaned `.prj` or `.qpj` whose WKT was donated is no longer
    // unusable - its content is in the ZIPs. Every one of them matches here
    // by construction (a donation requires them all to agree), and leaving
    // one behind would tell the user a file "has nothing to attach to"
    // moments after the same bytes were attached to seven bundles.
    const donatedPrjFiles = new Set(
        crsDonation ? crsFiles.filter((c) => c.key === donor?.key).map((c) => c.file) : [],
    );

    return {
        bundles,
        passthrough,
        unusable: [...unusable.filter((u) => !donatedPrjFiles.has(u.file)), ...rasterTabs],
        crsDonation,
    };
}

/** True if `ext` is a shapefile sidecar - used to explain why one was skipped. */
export function isShapefileSidecar(ext: string): boolean {
    return SIDECARS.has(ext.toLowerCase());
}

/** True if `ext` is a MapInfo sidecar - never an upload on its own. */
export function isMapInfoSidecar(ext: string): boolean {
    return MAPINFO_SIDECAR_SET.has(ext.toLowerCase());
}
