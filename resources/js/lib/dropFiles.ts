/**
 * Getting real files out of a dropped or picked folder.
 *
 * ## Why this is shared
 *
 * NewProject had a working directory walk and DataImportWizard had none at
 * all — its `onDrop` read `e.dataTransfer.files` and nothing else. A browser
 * puts a dropped FOLDER into that list as a single 0-byte `File` named after
 * the directory, so the wizard saw five files called `Apollo Sitka`,
 * `Centennial`, … , failed `ACCEPTED_EXTENSIONS.includes('Apollo Sitka')`, and
 * reported five "unsupported files" while 72 real files stayed on disk.
 *
 * ## The relative path is not optional
 *
 * `groupKey()` in shapefileBundle.ts keys a dataset on `webkitRelativePath` +
 * stem, precisely so a delivery holding `geology/faults.shp` and
 * `claims/faults.shp` produces two bundles instead of one scrambled one. A
 * directory `<input webkitdirectory>` sets that property; the drag-drop entry
 * API does NOT — every `File` it yields has `webkitRelativePath === ''`.
 *
 * So a walk that just collects Files hands the bundler a flat namespace and
 * silently merges datasets that happen to share a stem. This delivery has
 * `Veins.DAT` under `Sitka_CMap_12.6.2015` and `Veins.MAP` under
 * `2014 Polygons`; flattened, they group together as one "Veins" dataset.
 * We therefore stamp `webkitRelativePath` from the entry's own `fullPath`,
 * which makes a dropped folder behave exactly like a picked one.
 */

/** A dropped file plus where it came from, before we stamp the File itself. */
interface WalkedFile {
    file: File;
    /** Path relative to the drop root, e.g. `Apollo Sitka/Trench/TR005/x.tif`. */
    relativePath: string;
}

/**
 * `readEntries` returns at most 100 entries per call and signals completion
 * with an empty batch. Looping is mandatory: a single call on a 400-file
 * directory silently returns the first 100.
 */
function readAllEntries(reader: {
    readEntries: (ok: (e: unknown[]) => void, err: () => void) => void;
}): Promise<unknown[]> {
    return new Promise((resolve) => {
        reader.readEntries(
            (entries) => resolve(entries),
            () => resolve([]),
        );
    });
}

type FsEntry = {
    isFile?: boolean;
    isDirectory?: boolean;
    fullPath?: string;
    name?: string;
    file?: (ok: (f: File) => void, err: () => void) => void;
    createReader?: () => { readEntries: (ok: (e: unknown[]) => void, err: () => void) => void };
};

/**
 * Every file inside `entry`, at any depth, with its path relative to the drop.
 *
 * Depth is not capped. The delivery this was written against nests six levels
 * (`Centennial/Geophysics/IP/June 19/L3750N/export/`), and a cap is
 * indistinguishable from data loss to the person who dropped the folder.
 */
async function walkEntry(entry: FsEntry | null | undefined, prefix = ''): Promise<WalkedFile[]> {
    if (!entry) return [];

    const name = entry.name ?? '';
    const here = prefix ? `${prefix}/${name}` : name;

    if (entry.isFile && typeof entry.file === 'function') {
        const file = await new Promise<File | null>((resolve) => {
            entry.file!(
                (f) => resolve(f),
                () => resolve(null),
            );
        });
        // A file that will not open is reported by its absence from the
        // manifest rather than as a phantom entry with no bytes.
        return file ? [{ file, relativePath: here }] : [];
    }

    if (entry.isDirectory && typeof entry.createReader === 'function') {
        const reader = entry.createReader();
        const out: WalkedFile[] = [];
        for (;;) {
            const batch = await readAllEntries(reader);
            if (!batch.length) break;
            for (const child of batch) {
                out.push(...(await walkEntry(child as FsEntry, here)));
            }
        }
        return out;
    }

    return [];
}

/**
 * Give a File the `webkitRelativePath` the entry API withholds.
 *
 * `webkitRelativePath` is a read-only accessor on File.prototype, so it is
 * redefined on the instance rather than assigned. Files that already carry one
 * (a `webkitdirectory` pick) are left exactly as they are — overwriting a real
 * path with a reconstructed one is how the two intake routes would drift.
 */
function withRelativePath(file: File, relativePath: string): File {
    const existing = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
    if (existing) return file;
    try {
        Object.defineProperty(file, 'webkitRelativePath', {
            value: relativePath,
            configurable: true,
            enumerable: true,
        });
    } catch {
        // Non-configurable in some engine; grouping degrades to the flat
        // namespace, which is the pre-existing behaviour and not a new fault.
    }
    return file;
}

/**
 * Whether the browser handed us a folder disguised as a File.
 *
 * Used only to explain a drop the entry API could not read — a folder has no
 * type and no size, and telling the user "unsupported file type: Apollo Sitka"
 * is the message that hid 72 files.
 */
export function looksLikeFolder(f: File): boolean {
    return f.size === 0 && f.type === '';
}

/**
 * Every file in a drop, recursing into folders when the browser allows it.
 *
 * Falls back to `dataTransfer.files` when the entry API is unavailable, which
 * is the plain multi-file case and needs no walking.
 */
export async function filesFromDataTransfer(dt: DataTransfer | null | undefined): Promise<File[]> {
    if (!dt) return [];

    const items = dt.items;
    const canWalk =
        items &&
        items.length > 0 &&
        typeof (items[0] as DataTransferItem & { webkitGetAsEntry?: unknown }).webkitGetAsEntry ===
            'function';

    if (canWalk) {
        // Collect the entries BEFORE awaiting anything: the DataTransferItemList
        // is emptied when the drop event handler returns, so a loop that awaits
        // between reads finds `null` for every item after the first.
        const entries: FsEntry[] = [];
        for (let i = 0; i < items!.length; i++) {
            const entry = (
                items![i] as DataTransferItem & { webkitGetAsEntry?: () => FsEntry | null }
            ).webkitGetAsEntry?.();
            if (entry) entries.push(entry);
        }

        const walked: WalkedFile[] = [];
        for (const entry of entries) {
            walked.push(...(await walkEntry(entry)));
        }
        if (walked.length > 0) {
            return walked.map((w) => withRelativePath(w.file, w.relativePath));
        }
    }

    return dt.files ? Array.from(dt.files) : [];
}
