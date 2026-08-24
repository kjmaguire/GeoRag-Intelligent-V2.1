import { useCallback, useMemo, useRef, useState } from 'react';
import { Head } from '@inertiajs/react';
import JSZip from 'jszip';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card } from '@/Components/Foundry/primitives';
import {
    CATEGORY_EXTS,
    CATEGORY_LABEL,
    RETIRED_CATEGORIES,
    UNSUPPORTED_EXTS,
    categoryForExtension,
    extensionOf,
    parseEpsg,
    supportsCrsOverride,
    type Category,
} from '@/lib/uploadCategories';
import {
    bundleKey,
    dedupeFiles,
    fileKey,
    groupShapefiles,
    type CrsProvenance,
} from '@/lib/shapefileBundle';

const STEPS = ['Identity', 'Jurisdiction', 'Corpus', 'Review'] as const;
type Step = typeof STEPS[number];

const COUNTRIES = [
    { code: 'US', name: 'United States' },
    { code: 'CA', name: 'Canada' },
] as const;

const STATES_BY_COUNTRY: Record<string, Array<{ code: string; name: string }>> = {
    US: [
        { code: 'AK', name: 'Alaska' },
        { code: 'AZ', name: 'Arizona' },
        { code: 'CA', name: 'California' },
        { code: 'CO', name: 'Colorado' },
        { code: 'ID', name: 'Idaho' },
        { code: 'MI', name: 'Michigan' },
        { code: 'MN', name: 'Minnesota' },
        { code: 'MT', name: 'Montana' },
        { code: 'NM', name: 'New Mexico' },
        { code: 'NV', name: 'Nevada' },
        { code: 'OR', name: 'Oregon' },
        { code: 'SD', name: 'South Dakota' },
        { code: 'TX', name: 'Texas' },
        { code: 'UT', name: 'Utah' },
        { code: 'WA', name: 'Washington' },
        { code: 'WY', name: 'Wyoming' },
    ],
    CA: [
        { code: 'AB', name: 'Alberta' },
        { code: 'BC', name: 'British Columbia' },
        { code: 'MB', name: 'Manitoba' },
        { code: 'NB', name: 'New Brunswick' },
        { code: 'NL', name: 'Newfoundland & Labrador' },
        { code: 'NS', name: 'Nova Scotia' },
        { code: 'NT', name: 'Northwest Territories' },
        { code: 'NU', name: 'Nunavut' },
        { code: 'ON', name: 'Ontario' },
        { code: 'PE', name: 'Prince Edward Island' },
        { code: 'QC', name: 'Québec' },
        { code: 'SK', name: 'Saskatchewan' },
        { code: 'YT', name: 'Yukon' },
    ],
};

const COMMODITIES = ['Uranium', 'Gold', 'Copper', 'Nickel', 'Lithium', 'Zinc', 'Silver', 'Lead', 'REE'];

// Categories, labels and extensions live in one shared module so this
// picker, DataImportWizard and UploadController cannot drift apart again —
// they had, in both directions. See resources/js/lib/uploadCategories.ts.

const MAX_FILE_BYTES = 6 * 1024 * 1024 * 1024; // 6 GB — matches UploadController + Octane limits (ZIP archive support)


function humanSize(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/**
 * A coordinate system this bundle did not have, copied in from another file
 * in the same selection.
 *
 * The bundler's own type, aliased rather than restated, because the identity
 * of the recipient is the thing this must not get wrong. It travels ON the
 * bundle (`SpatialBundle.crsFrom`): bundle stems are NOT unique — a delivery
 * routinely holds `geology/faults.shp` and `claims/faults.shp`, which
 * groupShapefiles() deliberately keeps apart — so a screen that matched the
 * donation by stem credited it to the wrong row and stripped the wrong member,
 * or stripped nothing at all.
 *
 * Carried per row because that is the granularity the user acts at: the copy
 * can be dropped for one dataset by typing an EPSG code for it, or for the
 * whole selection with the control above the queue.
 */
type DonatedCrs = CrsProvenance;

/**
 * Rebuild a bundle ZIP without the donated `.prj`.
 *
 * The donation is one extra member copied into the archive under the
 * recipient's own stem, so removing that member leaves exactly the bundle the
 * grouper would have produced had it never donated — nothing else in the
 * archive is touched, and member names stay bare.
 *
 * This is what makes both the "do not apply it" control and a typed EPSG code
 * real rather than cosmetic. A CRS the FILE declares always wins server-side
 * (spatial_parser: `source_epsg` is applied ONLY when the file declares none),
 * so a copy left in the ZIP would quietly outrank the code the user typed.
 */
async function withoutDonatedPrj(bundle: File, memberName: string): Promise<File> {
    const zip = await JSZip.loadAsync(await bundle.arrayBuffer());
    // JSZip.remove() is a SILENT no-op when the entry is not in the archive.
    // Left unchecked, a member name that does not match ships the donated
    // `.prj` anyway — uploading the coordinate system the user just declined,
    // which is the one outcome this function exists to prevent. Both callers
    // turn a throw here into a visible failed row, so the wrong upload becomes
    // a message instead of a silent success.
    if (!zip.file(memberName)) {
        throw new Error(
            `${memberName} is not in this archive, so the copied coordinate system ` +
                'cannot be removed from it',
        );
    }
    zip.remove(memberName);
    const blob = await zip.generateAsync({ type: 'blob', compression: 'DEFLATE' });
    return new File([blob], bundle.name, { type: 'application/zip' });
}

interface DonationSummary {
    /** Eyebrow above the line. */
    headline: string;
    /** The line itself: what happened, from which file, to how many datasets. */
    detail: string;
    /** Label on the control that reverses it. */
    toggleLabel: string;
}

/**
 * The donation, in one sentence, in the same words on both upload screens.
 *
 * DataImportWizard.tsx carries a copy of this function verbatim. The two
 * screens have drifted before and it caused real bugs; if this wording
 * changes, both copies change together.
 *
 * @param donations One entry per bundle that received a copy.
 * @param overridden How many of those rows carry an EPSG code the user typed,
 *   which replaces the copy for that row.
 * @param wktUsing How many of the rows the donation actually reaches take it
 *   as TEXT (a lone .dxf/.dgn) rather than as a ZIP member. Declining the
 *   donation has a different consequence for those: the ingest does not
 *   refuse a CAD file, it stores the features as 'assumed' — and a banner
 *   threatening a refusal that will not happen is the unfollowable-advice
 *   bug this whole feature exists to remove.
 */
function donationSummary(
    donations: DonatedCrs[],
    overridden: number,
    enabled: boolean,
    wktUsing: number = 0,
): DonationSummary {
    const sources = [...new Set(donations.map((d) => d.sourceName))];
    const labels = [...new Set(donations.map((d) => d.label))];
    const source = sources.length === 1 ? sources[0] : `${sources.length} .prj files`;
    const label = labels.length === 1 ? labels[0] : `${labels.length} coordinate systems`;
    // Rows the copy actually reaches: the ones with no EPSG code of their own.
    const using = donations.length - overridden;
    const overrideNote =
        overridden > 0
            ? ` The ${overridden} row${overridden === 1 ? '' : 's'} with an EPSG code typed in use that code instead.`
            : '';

    if (!enabled) {
        const memberUsing = using - wktUsing;
        const consequence =
            memberUsing > 0 && wktUsing > 0
                ? "a shapefile without one is refused, and a CAD drawing is stored as 'assumed' with its position uncertain"
                : wktUsing > 0
                  ? `${wktUsing === 1 ? 'its' : 'their'} features are stored as 'assumed' with ${wktUsing === 1 ? 'its' : 'their'} position uncertain`
                  : `the ingest will refuse ${using === 1 ? 'it' : 'them'}`;
        return {
            headline: 'Coordinate system NOT applied',
            detail:
                using === 0
                    ? `${label} from ${source} is not being copied anywhere.${overrideNote}`
                    : `${label} from ${source} is not being copied. ` +
                      `${using === 1 ? 'One dataset' : `${using} datasets`} will upload declaring no ` +
                      `coordinate system — set an EPSG code on ${using === 1 ? 'it' : 'each of them'}, ` +
                      `or ${consequence}.${overrideNote}`,
            toggleLabel: using === 0 ? 'Apply it' : `Apply it to ${using} dataset${using === 1 ? '' : 's'}`,
        };
    }
    return {
        headline: 'Coordinate system applied',
        detail:
            using === 0
                ? `${label} was read from ${source}, and no dataset is taking a copy.${overrideNote}`
                : `${label}, read from ${source} — the only coordinate system this selection ` +
                  `declares — is being copied into ` +
                  `${using === 1 ? 'one dataset that arrived with no .prj of its own' : `${using} datasets that arrived with no .prj of their own`}, ` +
                  'so you do not have to type the same EPSG code once per file. The ingest still ' +
                  `measures it against the geometry and flags it if it does not fit.${overrideNote}`,
        toggleLabel: 'Do not apply it',
    };
}

interface QueuedFile {
    id: string;
    file: File;
    name: string;
    size: number;
    ext: string;
    /**
     * Identity that survives re-grouping — `bundleKey`/`fileKey`.
     *
     * Every added batch re-groups the whole selection, which rebuilds each
     * bundle's ZIP and so gives it a new `File`. This is what carries a
     * row's category and EPSG edits across that rebuild, and what stops an
     * already-uploaded row being queued a second time.
     */
    selectionKey?: string;
    /**
     * The selected files behind this row — a bundle's members, or the file
     * itself. Read when the row is removed, so its sources leave the
     * accumulated selection with it.
     */
    sources?: File[];
    category: Category | null; // null = unsupported
    status: 'queued' | 'uploading' | 'done' | 'error';
    error?: string;
    /** Advisory note shown beside the row. Not a failure — the file still uploads. */
    hint?: string;
    /**
     * CRS the user asserts for this file, as typed. Integer EPSG only.
     *
     * Carried per file, on the same struct as `category`, because an override
     * re-derived at submit time attaches itself to whichever row happens to
     * be in that position by then.
     *
     * A HINT, not a command: a file that declares its own coordinate system
     * keeps it, and the server measures the geometry against the claimed code
     * rather than trusting it.
     */
    sourceEpsgText?: string;
    /**
     * Set when this bundle had no `.prj` and was given a copy of the one
     * coordinate system the selection agreed on.
     *
     * Not a note, and deliberately not `error` or `hint`: it changes what is
     * inside the ZIP, so it is also what `submit()` reads to decide whether to
     * strip that copy back out.
     */
    crsDonation?: DonatedCrs;
    parentZip?: string; // set when this file was extracted from an uploaded archive
}

function newId(): string {
    return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

export default function FoundryNewProject() {
    const [step, setStep] = useState<Step>('Identity');
    const stepIdx = STEPS.indexOf(step);
    const [form, setForm] = useState({
        name: '',
        code: '',
        commodity: '',
        operator: '',
        country: '',
        state: '',
    });
    const setField = <K extends keyof typeof form>(k: K, v: (typeof form)[K]) =>
        setForm((f) => ({ ...f, [k]: v }));

    // Reset state when country changes so a stale selection (e.g. WY while CA
    // is now selected) can't be submitted.
    const setCountry = (code: string) =>
        setForm((f) => ({ ...f, country: code, state: '' }));

    const [queue, setQueue] = useState<QueuedFile[]>([]);
    /**
     * Every file the user has selected so far, across every drop and pick.
     *
     * A ref, not state: `addFiles` reads AND writes it within one call, and
     * a state value captured by the callback would be a batch behind — the
     * exact staleness that makes a second drop group in isolation.
     */
    const selectedFilesRef = useRef<File[]>([]);
    /** The live queue, for reading per-row edits during a regroup. */
    const queueRef = useRef<QueuedFile[]>([]);
    queueRef.current = queue;
    const fileInputRef = useRef<HTMLInputElement | null>(null);
    // Use a ref callback to set webkitdirectory directly on the DOM node —
    // React JSX doesn't reliably pass non-standard attributes through to the
    // DOM in all browser/version combinations.
    const folderInputRef = useCallback((node: HTMLInputElement | null) => {
        if (node) {
            (node as any).webkitdirectory = true;
            (node as any).directory = true; // Edge/IE fallback
        }
    }, []);
    const [dragging, setDragging] = useState(false);
    const [submitting, setSubmitting] = useState(false);
    const [submitError, setSubmitError] = useState<string | null>(null);
    const [submitProgress, setSubmitProgress] = useState<{ done: number; total: number } | null>(null);
    const [skipped, setSkipped] = useState<{ names: string[] } | null>(null);
    /** Incomplete-set verdicts and orphaned bundle members, each with the
     *  reason. Separate from `skipped`, which means "we do not accept this
     *  format at all" — a very different thing to tell a geologist. */
    const [bundleNotes, setBundleNotes] = useState<string[]>([]);
    /**
     * Whether the coordinate system the bundler copied into the CRS-less
     * bundles is used.
     *
     * On by default: the whole point is to stop asking a geologist to type
     * the same EPSG code seven times when the answer is sitting in the folder
     * they just dropped.
     *
     * The server is a check on that, not a guarantee. It scores the geometry
     * against whatever CRS the file arrives with and adds a
     * `crs_low_confidence` warning to the run when that score falls below its
     * threshold; the features are written either way. So a donated CRS that
     * does not fit is likely to be FLAGGED, not caught — which is why this is
     * reversible here, in front of the person who knows the ground: a CRS the
     * file did not declare is not the same fact as one it did.
     */
    const [donateCrs, setDonateCrs] = useState(true);

    const addFiles = useCallback(async (files: FileList | File[]) => {
        const arr: QueuedFile[] = [];
        const skippedNames: string[] = [];

        // A shapefile arrives as .shp + .shx/.dbf/.prj siblings. None of the
        // sidecars has an upload category, so the loop below used to drop them
        // as "unrecognised" and queue the .shp alone - which the server can
        // never parse ("Unable to open <name>.shx"). Zip each group back
        // together first; the spatial workflow already reads that shape.
        // Falling back to the raw list keeps a zip failure (out of memory on a
        // very large .dbf, say) from swallowing the whole selection.
        //
        // Grouping runs over the WHOLE accumulated selection, not just this
        // batch. Per-batch grouping is what stranded seven shapefiles'
        // attribute tables on 2026-08-24: a `.dbf` picked up in a second
        // drop has no `.shp` in ITS batch, so it was treated as a standalone
        // dBASE table and uploaded on its own. The bundles reaching storage
        // held `.shp` + `.prj` and nothing else, and the ingest reported
        // them as imported.
        const all = dedupeFiles([...selectedFilesRef.current, ...Array.from(files)]);
        selectedFilesRef.current = all;
        const { bundles, passthrough, unusable, wktRecipients } = await groupShapefiles(
            all,
        ).catch(() => ({
            bundles: [],
            passthrough: all,
            unusable: [],
            crsDonation: null,
            wktRecipients: [],
        }));

        // Per-row edits, carried across the rebuild by a key that survives
        // re-zipping. Without this, adding one file after setting eight
        // categories would reset all eight.
        const edits = new Map<string, { category?: Category | null; sourceEpsgText?: string }>();
        // Rows that have left the queue — uploading, uploaded, or failed.
        // They keep their existing entry and must NOT be rebuilt from the
        // regroup as well, or a finished upload appears twice and is sent
        // again.
        const settled = new Set<string>();
        for (const item of queueRef.current) {
            if (item.selectionKey === undefined) continue;
            edits.set(item.selectionKey, {
                category: item.category,
                sourceEpsgText: item.sourceEpsgText,
            });
            if (item.status !== 'queued') settled.add(item.selectionKey);
        }
        const notes: string[] = [];
        for (const b of bundles) {
            // Read off the bundle, never matched by stem. Stems are not
            // unique — two folders in one delivery can each hold a
            // `faults.shp`, and the grouper keeps them apart on purpose — so
            // a stem lookup could credit this row with a donation another
            // bundle received and then strip a member by a name that is not
            // in this archive. `crsFrom` carries the exact entry the grouper
            // wrote, which is the only name safe to remove again.
            const donated: DonatedCrs | undefined = b.crsFrom ?? undefined;
            const key = bundleKey(b);
            if (settled.has(key)) continue;
            const prior = edits.get(key);
            arr.push({
                id: newId(),
                file: b.file,
                name: b.file.name,
                size: b.file.size,
                ext: 'zip',
                selectionKey: key,
                sources: b.sources,
                category: prior?.category ?? 'spatial',
                sourceEpsgText: prior?.sourceEpsgText,
                status: 'queued',
                crsDonation: donated,
                // `hint`, NOT `error`. This used to be written to `error`,
                // which renders danger-red, truncates at 40 characters and is
                // overwritten by the first upload failure — three wrong
                // behaviours for a note about a file that uploads fine. GDAL
                // rebuilds a missing index by itself, and the consequences of
                // a missing .prj or .dbf are spelled out in `verdict`.
                hint:
                    b.verdict ??
                    (b.missing.length > 0
                        ? `Bundled ${b.members.length} files; no .${b.missing.join(', .')} found`
                        : undefined),
            });
            // A set that was given its coordinate system is not an incomplete
            // one, and listing seven of them under a heading that asks for
            // missing files is the noise this change exists to remove: it is
            // reported on its own line above instead. Anything ELSE the set is
            // missing (a .dbf) still belongs here — `missing` has already had
            // `prj` removed for a recipient, and a missing `.shx` has never
            // been worth a word.
            const stillIncomplete = !donated || b.missing.some((e) => e !== 'shx');
            if (b.verdict && stillIncomplete) notes.push(`${b.stem}: ${b.verdict}`);
        }
        // A member whose master was not selected is kept and explained, not
        // filed under "unrecognised format". A standalone .dbf never lands
        // here — it is an attribute table and comes back in `passthrough`.
        for (const u of unusable) notes.push(`${u.file.name}: ${u.reason}`);

        const wktCrsByFile = new Map<File, DonatedCrs>(
            wktRecipients.map((r): [File, DonatedCrs] => [r.file, r.crs]),
        );
        for (const f of passthrough) {
            const ext = extensionOf(f.name);
            if (settled.has(fileKey(f))) continue;
            // Drop files we can't categorise (unknown extension, raster images,
            // or 0-byte folder shells dragged in instead of using Select Folder).
            // Keep ZIPs — they're handled below with their own error message.
            if (ext !== 'zip' && (categoryForExtension(ext) === null || f.size === 0)) {
                skippedNames.push(f.name);
                continue;
            }
            // A ZIP is queued rather than extracted in-browser (memory limit
            // on large archives), so the server has to route it.
            //
            // `archive`, not `spatial`. This used to hardcode 'spatial' for
            // every ZIP, which sent it to ingest_spatial — a workflow whose
            // extractor returns only members with a vector or QGIS suffix.
            // A field-season ZIP of 180 PDFs and 40 LAS files therefore
            // yielded zero members, logged one `archive_has_no_vector_data`
            // warning, wrote nothing, and reported the run COMPLETED. All
            // 220 files were gone with no error anywhere the user could see.
            //
            // ingest_zip_archive handles a mixed archive: it fans each member
            // out to the right ingester, including vector data (added
            // 2026-08-21). The per-file `<select>` below still lets a user
            // choose `spatial` for a ZIP they know is a shapefile bundle.
            if (ext === 'zip') {
                arr.push({
                    id: newId(),
                    file: f,
                    name: f.name,
                    size: f.size,
                    ext,
                    selectionKey: fileKey(f),
                    category: edits.get(fileKey(f))?.category ?? 'archive',
                    sourceEpsgText: edits.get(fileKey(f))?.sourceEpsgText,
                    // 'queued', not 'error': this file IS uploaded and IS
                    // processed. Extracting first still gives better
                    // per-file progress, which is what the hint is for.
                    status: 'queued',
                    hint: 'Uploaded as an archive. For per-file progress, extract it first and use “Select Folder”.',
                });
                continue;
            }
            const key = fileKey(f);
            const prior = edits.get(key);
            arr.push({
                id: newId(),
                file: f,
                name: f.name,
                size: f.size,
                ext,
                selectionKey: key,
                category: prior?.category ?? categoryForExtension(ext),
                sourceEpsgText: prior?.sourceEpsgText,
                status: 'queued',
                // Matched by File identity, never by name — same rule as the
                // bundles above. Set for a lone .dxf/.dgn beside the
                // delivery's one agreed .prj: the donation rides the upload
                // as `source_crs_wkt`, since there is no ZIP to copy it into.
                crsDonation: wktCrsByFile.get(f),
            });
        }
        // Replaced, not appended: `arr` is the whole selection re-grouped,
        // so appending would queue every earlier file a second time. Rows
        // that already uploaded are kept — re-zipping a finished upload
        // would send it again.
        setQueue((q) => [...q.filter((x) => x.status !== 'queued'), ...arr]);
        // Both of these are REPLACED rather than accumulated: `notes` and
        // `skippedNames` are regenerated for the whole selection on every
        // regroup, so appending would repeat every earlier line and keep
        // showing verdicts for sets that a later drop has since completed.
        setSkipped(skippedNames.length > 0 ? { names: skippedNames } : null);
        setBundleNotes(notes);
    }, []);

    const removeFile = (id: string) => {
        // Un-select the row's SOURCE files too. Dropping only the queue row
        // leaves them in `selectedFilesRef`, and the next added file
        // re-groups the whole selection and brings the removed row back.
        const target = queueRef.current.find((x) => x.id === id);
        if (target) {
            const gone = new Set((target.sources ?? [target.file]).map(fileKey));
            selectedFilesRef.current = selectedFilesRef.current.filter(
                (f) => !gone.has(fileKey(f)),
            );
        }
        setQueue((q) => q.filter((x) => x.id !== id));
    };
    const setCategory = (id: string, cat: Category) =>
        setQueue((q) => q.map((x) => (x.id === id ? { ...x, category: cat } : x)));
    const setSourceEpsg = (id: string, text: string) =>
        setQueue((q) => q.map((x) => (x.id === id ? { ...x, sourceEpsgText: text } : x)));

    // Recursively walk a dropped directory entry, returning every File inside.
    // Browser drag-drop exposes folders as 0-byte File objects in
    // `dataTransfer.files`; the real contents only come out via the
    // DataTransferItem `webkitGetAsEntry()` API + `directoryReader.readEntries`.
    // Note: readEntries returns at most 100 entries per call, so we loop until
    // it returns empty (otherwise large folders silently truncate).
    const walkEntry = useCallback(async (entry: any): Promise<File[]> => {
        if (!entry) return [];
        if (entry.isFile) {
            return new Promise<File[]>((resolve) => {
                entry.file(
                    (f: File) => resolve([f]),
                    () => resolve([]),
                );
            });
        }
        if (entry.isDirectory) {
            const reader = entry.createReader();
            const out: File[] = [];
            while (true) {
                const batch: any[] = await new Promise((resolve) => {
                    reader.readEntries(
                        (entries: any[]) => resolve(entries),
                        () => resolve([]),
                    );
                });
                if (!batch.length) break;
                for (const child of batch) {
                    const files = await walkEntry(child);
                    out.push(...files);
                }
            }
            return out;
        }
        return [];
    }, []);

    const onDrop = useCallback(
        async (e: React.DragEvent<HTMLDivElement>) => {
            e.preventDefault();
            setDragging(false);
            const items = e.dataTransfer?.items;
            // Prefer the items API when available — it lets us recurse into
            // dropped folders. Fall back to dataTransfer.files when not.
            if (items && items.length > 0 && typeof items[0].webkitGetAsEntry === 'function') {
                const collected: File[] = [];
                const entries: any[] = [];
                for (let i = 0; i < items.length; i++) {
                    const entry = items[i].webkitGetAsEntry?.();
                    if (entry) entries.push(entry);
                }
                for (const entry of entries) {
                    const files = await walkEntry(entry);
                    collected.push(...files);
                }
                if (collected.length > 0) {
                    addFiles(collected);
                    return;
                }
            }
            if (e.dataTransfer?.files?.length) addFiles(e.dataTransfer.files);
        },
        [addFiles, walkEntry],
    );

    const queueSummary = useMemo(() => {
        const ok = queue.filter((q) => q.category !== null && q.size <= MAX_FILE_BYTES);
        const unsupported = queue.filter((q) => q.category === null);
        const oversize = queue.filter((q) => q.category !== null && q.size > MAX_FILE_BYTES);
        const bytes = ok.reduce((s, q) => s + q.size, 0);
        // An EPSG the API would refuse blocks the create button rather than
        // being dropped on the way out. Silently discarding a value the user
        // typed is the failure mode this whole change set is about.
        const badEpsg = queue.filter(
            (q) =>
                supportsCrsOverride(q.category) &&
                parseEpsg(q.sourceEpsgText ?? '').error !== undefined,
        );
        return { ok, unsupported, oversize, bytes, badEpsg };
    }, [queue]);

    /**
     * True when this row carries an EPSG code the upload will actually send.
     *
     * On a row that was given a donated `.prj`, that code is only obeyed if
     * the copy is removed first — the file's own declaration outranks
     * `source_epsg` server-side — so this is also the test for stripping it.
     */
    function hasExplicitEpsg(q: QueuedFile): boolean {
        return (
            supportsCrsOverride(q.category) &&
            parseEpsg(q.sourceEpsgText ?? '').epsg !== undefined
        );
    }

    /** True when the copied coordinate system is what this row will upload with. */
    function donationInEffect(q: QueuedFile): boolean {
        return q.crsDonation !== undefined && donateCrs && !hasExplicitEpsg(q);
    }

    const donationRows = queue.filter((q) => q.crsDonation !== undefined);
    const donationOverrides = donationRows.filter(hasExplicitEpsg).length;
    // WKT-carriage recipients (lone .dxf/.dgn) among the rows the donation
    // actually reaches: the banner's toggle-off consequence differs — a CAD
    // file is never refused, it lands as 'assumed'.
    const donationWktUsing = donationRows.filter(
        (q) => !hasExplicitEpsg(q) && q.crsDonation?.memberName === undefined,
    ).length;
    const donation =
        donationRows.length > 0
            ? donationSummary(
                  donationRows.map((q) => q.crsDonation as DonatedCrs),
                  donationOverrides,
                  donateCrs,
                  donationWktUsing,
              )
            : null;

    function next() {
        const i = STEPS.indexOf(step);
        if (i < STEPS.length - 1) setStep(STEPS[i + 1]);
    }
    function back() {
        const i = STEPS.indexOf(step);
        if (i > 0) setStep(STEPS[i - 1]);
    }

    async function submit() {
        setSubmitting(true);
        setSubmitError(null);
        try {
            const csrf =
                document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') ?? null;
            const headers: Record<string, string> = {
                Accept: 'application/json',
            };
            if (csrf) headers['X-CSRF-TOKEN'] = csrf;

            // 1. Create project — matches POST /api/v1/projects (ProjectController@store).
            // Body fields mirror what Pages/NewProject.tsx already sends.
            const createRes = await fetch('/api/v1/projects', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { ...headers, 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    project_name: form.name,
                    company: form.operator,
                    commodity: form.commodity,
                    // region carries the state/province code (e.g. "WY", "ON")
                    // to match existing seeded projects. Country scopes the UI
                    // picker but isn't a separate column on silver.projects.
                    region: form.state,
                    orientation_reference: 'BOH',
                }),
            });
            const createJson = await createRes.json().catch(() => ({}));
            if (!createRes.ok) {
                throw new Error(createJson.message || `Project create failed (HTTP ${createRes.status})`);
            }
            const projectId: string | undefined =
                createJson.data?.project_id ?? createJson.project_id;
            const projectSlug: string | undefined =
                createJson.data?.slug ?? createJson.slug;
            if (!projectId) throw new Error('Project created but no project_id returned.');

            // 2. Upload each queued file. Skip unsupported + oversize; flag them.
            // status !== 'error' matters: the queue can hold a row the UI has
            // already told the user is unusable, and this filter used to
            // ignore that and upload it anyway.
            const uploadable = queue.filter(
                (q) => q.category !== null && q.size <= MAX_FILE_BYTES && q.status !== 'error',
            );
            setSubmitProgress({ done: 0, total: uploadable.length });

            let done = 0;
            for (const qf of uploadable) {
                setQueue((q) => q.map((x) => (x.id === qf.id ? { ...x, status: 'uploading' } : x)));
                // `source_epsg`, an integer, and only for a category whose
                // trigger carries it. Same field name and same type as the
                // one the tabular ingest already takes: one concept, one
                // spelling, across the two paths this screen feeds.
                const epsg = parseEpsg(qf.sourceEpsgText ?? '');
                const explicitEpsg =
                    supportsCrsOverride(qf.category) && epsg.epsg !== undefined;

                // The donated `.prj` is taken back out of the archive when the
                // user turned the donation off, or when they typed a code for
                // this row. Both have to change the bytes, not just the text:
                // a CRS the file declares always beats `source_epsg`, so a copy
                // left in the ZIP would outrank the code the user typed and the
                // override would look accepted while doing nothing.
                const donated = qf.crsDonation;
                let payload = qf.file;
                if (donated?.memberName && (!donateCrs || explicitEpsg)) {
                    try {
                        payload = await withoutDonatedPrj(qf.file, donated.memberName);
                    } catch (err) {
                        // Uploading the copy anyway would be uploading the
                        // coordinate system the user just declined.
                        const why = err instanceof Error ? err.message : String(err);
                        const member = donated.memberName;
                        setQueue((q) =>
                            q.map((x) =>
                                x.id === qf.id
                                    ? {
                                          ...x,
                                          status: 'error',
                                          error: `Could not drop the copied ${member}: ${why}`,
                                      }
                                    : x,
                            ),
                        );
                        done += 1;
                        setSubmitProgress({ done, total: uploadable.length });
                        continue;
                    }
                }

                const fd = new FormData();
                fd.append('file', payload);
                fd.append('category', qf.category as string);
                if (explicitEpsg && epsg.epsg !== undefined) {
                    fd.append('source_epsg', String(epsg.epsg));
                }
                // A WKT-carriage recipient (a lone .dxf/.dgn — no ZIP to
                // hold a copied .prj) sends the donation as text; the server
                // resolves it with pyproj. Declining it or typing a code
                // means simply not sending it.
                if (donated?.wkt && donateCrs && !explicitEpsg) {
                    fd.append('source_crs_wkt', donated.wkt);
                }
                try {
                    const upRes = await fetch(`/api/v1/projects/${projectId}/upload`, {
                        method: 'POST',
                        credentials: 'same-origin',
                        headers, // no Content-Type — let the browser set the multipart boundary
                        body: fd,
                    });
                    const upJson = await upRes.json().catch(() => ({}));
                    if (!upRes.ok) {
                        throw new Error(upJson.message || `HTTP ${upRes.status}`);
                    }
                    setQueue((q) =>
                        q.map((x) => (x.id === qf.id ? { ...x, status: 'done' } : x)),
                    );
                } catch (err) {
                    const msg = err instanceof Error ? err.message : String(err);
                    setQueue((q) =>
                        q.map((x) =>
                            x.id === qf.id ? { ...x, status: 'error', error: msg } : x,
                        ),
                    );
                }
                done += 1;
                setSubmitProgress({ done, total: uploadable.length });
            }

            // 3. Land the user on Ingestion Runs, not the bare Overview —
            // OCR/embedding is async (Hatchet workflow) and wasn't done yet
            // just because the upload loop finished. Overview only picks up
            // ingestion progress via its own 5s/30s poll banner, reached a
            // full navigation later than this; Ingestion Runs shows live
            // per-file OCR/parse/embed status immediately (5s poll, see
            // routes/web.php's foundry.ingestion-runs route), closing the
            // gap between "upload finished" and "processing is visible".
            window.location.href = `/projects/${projectSlug ?? projectId}/ingestion-runs`;
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            setSubmitError(msg);
            setSubmitting(false);
            setSubmitProgress(null);
        }
    }

    return (
        <AppLayout>
            <Head title="New project — GeoRAG" />

            <div className="flex-1 overflow-y-auto" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader eyebrow="NEW PROJECT" title="Create a project" sub={`Step ${stepIdx + 1} of ${STEPS.length}: ${step}`} />

                <div className="max-w-2xl mx-auto px-8 py-6">
                    {/* Stepper */}
                    <ol className="flex items-center gap-2 mb-6">
                        {STEPS.map((s, i) => (
                            <li key={s} className="flex items-center gap-2">
                                <span
                                    className="w-6 h-6 rounded-full text-[10px] font-mono flex items-center justify-center"
                                    style={{
                                        background: i <= stepIdx ? 'var(--accent-bg)' : 'var(--bg-2)',
                                        color: i <= stepIdx ? 'var(--accent)' : 'var(--fg-3)',
                                        border: '1px solid ' + (i <= stepIdx ? 'var(--accent-dim)' : 'var(--line-1)'),
                                    }}
                                >
                                    {i + 1}
                                </span>
                                <span className="text-[11px] font-mono uppercase tracking-wider" style={{ color: i === stepIdx ? 'var(--fg-0)' : 'var(--fg-3)' }}>{s}</span>
                                {i < STEPS.length - 1 && <span style={{ color: 'var(--fg-3)' }}>›</span>}
                            </li>
                        ))}
                    </ol>

                    <Card eyebrow={`STEP ${stepIdx + 1}`} title={step}>
                        {step === 'Identity' && (
                            <div className="space-y-3">
                                <Field label="Project name" required>
                                    <input type="text" value={form.name} onChange={(e) => setField('name', e.target.value)} className="w-full text-sm px-3 py-2 rounded border" style={inputStyle} />
                                </Field>
                                <Field label="Project code">
                                    <input type="text" value={form.code} onChange={(e) => setField('code', e.target.value)} className="w-full text-sm px-3 py-2 rounded border" style={inputStyle} />
                                </Field>
                                <Field label="Operator">
                                    <input type="text" value={form.operator} onChange={(e) => setField('operator', e.target.value)} className="w-full text-sm px-3 py-2 rounded border" style={inputStyle} />
                                </Field>
                                <Field label="Commodity">
                                    <select value={form.commodity} onChange={(e) => setField('commodity', e.target.value)} className="text-sm px-3 py-2 rounded border" style={inputStyle}>
                                        <option value="">— select —</option>
                                        {COMMODITIES.map((c) => <option key={c} value={c.toLowerCase()}>{c}</option>)}
                                    </select>
                                </Field>
                            </div>
                        )}
                        {step === 'Jurisdiction' && (
                            <div className="space-y-3">
                                <Field label="Country">
                                    <select value={form.country} onChange={(e) => setCountry(e.target.value)} className="text-sm px-3 py-2 rounded border" style={inputStyle}>
                                        <option value="">— select —</option>
                                        {COUNTRIES.map((c) => <option key={c.code} value={c.code}>{c.name}</option>)}
                                    </select>
                                </Field>
                                <Field label={form.country === 'CA' ? 'Province / Territory' : 'State'}>
                                    <select
                                        value={form.state}
                                        onChange={(e) => setField('state', e.target.value)}
                                        disabled={!form.country}
                                        className="text-sm px-3 py-2 rounded border disabled:opacity-50"
                                        style={inputStyle}
                                    >
                                        <option value="">{form.country ? '— select —' : '— select country first —'}</option>
                                        {(STATES_BY_COUNTRY[form.country] ?? []).map((s) => (
                                            <option key={s.code} value={s.code}>{s.name}</option>
                                        ))}
                                    </select>
                                </Field>
                            </div>
                        )}
                        {step === 'Corpus' && (
                            <div className="space-y-4">
                                <p className="text-xs" style={{ color: 'var(--fg-2)' }}>
                                    Queue any files you already have. Once the project is created they're streamed to the bronze
                                    bucket and picked up by the Dagster ingestion sensor within ~5&nbsp;minutes.
                                    Per-file cap: 6&nbsp;GB.
                                </p>

                                {/* Drop zone — click opens individual file picker */}
                                <div
                                    onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
                                    onDragLeave={() => setDragging(false)}
                                    onDrop={onDrop}
                                    onClick={() => fileInputRef.current?.click()}
                                    role="button"
                                    tabIndex={0}
                                    onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') fileInputRef.current?.click(); }}
                                    className="rounded-md border-2 border-dashed text-center cursor-pointer transition-colors px-4 py-6"
                                    style={{
                                        borderColor: dragging ? 'var(--accent)' : 'var(--line-2)',
                                        background: dragging ? 'var(--accent-bg)' : 'var(--bg-2)',
                                    }}
                                >
                                    <div className="text-sm font-medium mb-1" style={{ color: 'var(--fg-0)' }}>
                                        {dragging ? 'Release to add files' : 'Drag files here, or click to browse'}
                                    </div>
                                    <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                                        {/* KMZ, SEG-Y and XYZ were listed here and none of
                                            them is accepted; the label promised uploads that
                                            came back 422. Shapefile and MapInfo sidecars are
                                            named because dropping the whole set is what gets
                                            the .prj — and therefore the coordinate system —
                                            to the server. */}
                                        CSV · PDF · TIFF · LAS · XLSX · GeoJSON · SHP + .shx/.dbf/.prj · MapInfo TAB/MIF · DBF · GPKG · ZIP
                                    </div>
                                    <input
                                        ref={fileInputRef}
                                        type="file"
                                        multiple
                                        className="sr-only"
                                        onChange={(e) => {
                                            if (e.target.files?.length) addFiles(e.target.files);
                                            e.target.value = '';
                                        }}
                                    />
                                </div>

                                {/* Folder picker — completely separate from the drop zone to avoid nested click conflicts */}
                                <label className="flex items-center justify-center gap-2 rounded-md border cursor-pointer transition-colors px-4 py-3"
                                    style={{ borderColor: 'var(--line-2)', background: 'var(--bg-2)' }}
                                >
                                    <span className="text-sm font-medium" style={{ color: 'var(--fg-0)' }}>📁 Select Folder</span>
                                    <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                                        — pick a directory, all files load automatically
                                    </span>
                                    <input
                                        ref={folderInputRef}
                                        type="file"
                                        multiple
                                        className="sr-only"
                                        onChange={(e) => {
                                            if (e.target.files?.length) addFiles(e.target.files);
                                            e.target.value = '';
                                        }}
                                    />
                                </label>

                                {/* Cloud URL — stubbed; backend doesn't accept paste-URL yet */}
                                <div
                                    className="flex items-center gap-2 rounded border px-3 py-2"
                                    style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)', opacity: 0.6 }}
                                    title="Cloud URL fetch isn't wired yet. Upload local files through the import wizard."
                                >
                                    <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>Cloud URL</span>
                                    <input
                                        type="text"
                                        disabled
                                        placeholder="s3://… · https://… (coming soon)"
                                        className="flex-1 text-xs bg-transparent outline-none font-mono"
                                        style={{ color: 'var(--fg-3)' }}
                                    />
                                    <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>soon</span>
                                </div>

                                {/* Skipped-file notice (unrecognised extension / 0-byte folder shells) */}
                                {skipped && skipped.names.length > 0 && (
                                    <div
                                        className="flex items-start gap-2 rounded border px-3 py-2 text-[11px]"
                                        style={{ borderColor: 'var(--warn, oklch(0.78 0.18 75))', color: 'var(--warn, oklch(0.78 0.18 75))', background: 'var(--bg-1)' }}
                                    >
                                        <div className="flex-1 min-w-0">
                                            <div className="font-mono uppercase tracking-wider text-[10px]">
                                                Skipped {skipped.names.length} file{skipped.names.length === 1 ? '' : 's'} · unrecognised format or empty folder
                                            </div>
                                            <div className="truncate" title={skipped.names.join(', ')} style={{ color: 'var(--fg-2)' }}>
                                                {skipped.names.slice(0, 3).join(', ')}
                                                {skipped.names.length > 3 && ` … +${skipped.names.length - 3} more`}
                                            </div>
                                            <div className="text-[10px]" style={{ color: 'var(--fg-3)' }}>
                                                Tip: if you dragged a folder, use 📁 Select Folder instead so its contents are enumerated.
                                            </div>
                                        </div>
                                        <button
                                            type="button"
                                            onClick={() => setSkipped(null)}
                                            className="text-[11px] px-2 py-0.5"
                                            style={{ color: 'var(--fg-3)' }}
                                            aria-label="Dismiss skipped-files notice"
                                        >
                                            ✕
                                        </button>
                                    </div>
                                )}

                                {/* The one coordinate system this selection declared,
                                    copied into the bundles that declared none. It sits
                                    ABOVE the list below because it is the reason that
                                    list is short: without it, every CRS-less shapefile
                                    in a real delivery is another row asking the user
                                    for a code the folder already contains. Applied by
                                    default, named out loud, and reversible. */}
                                {donation && (
                                    <div
                                        className="rounded border px-3 py-2 text-[11px]"
                                        style={{
                                            borderColor: donateCrs
                                                ? 'var(--accent-dim)'
                                                : 'var(--warn, oklch(0.78 0.18 75))',
                                            background: donateCrs ? 'var(--accent-bg)' : 'var(--bg-1)',
                                        }}
                                    >
                                        <div
                                            className="font-mono uppercase tracking-wider text-[10px]"
                                            style={{
                                                color: donateCrs
                                                    ? 'var(--accent)'
                                                    : 'var(--warn, oklch(0.78 0.18 75))',
                                            }}
                                        >
                                            {donation.headline}
                                        </div>
                                        <div className="mt-0.5" style={{ color: 'var(--fg-2)' }}>
                                            {donation.detail}
                                        </div>
                                        <button
                                            type="button"
                                            onClick={() => setDonateCrs((v) => !v)}
                                            disabled={submitting}
                                            aria-pressed={donateCrs}
                                            className="mt-1.5 text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border disabled:opacity-40"
                                            style={{
                                                color: 'var(--fg-2)',
                                                borderColor: 'var(--line-2)',
                                                background: 'var(--bg-2)',
                                            }}
                                        >
                                            {donation.toggleLabel}
                                        </button>
                                    </div>
                                )}

                                {/* Sets that need something the selection did not
                                    contain, and members whose master was never sent.
                                    NOT the same thing as the skipped notice above:
                                    these are formats we accept, and nothing here was
                                    thrown away — the bundles are in the queue below.
                                    Real deliveries are messy; a bundler that answered
                                    "no master, therefore drop" silently lost seven
                                    .DAT files, three .MAP files, an .ID and an .IND
                                    out of one folder. */}
                                {bundleNotes.length > 0 && (
                                    <div
                                        className="flex items-start gap-2 rounded border px-3 py-2 text-[11px]"
                                        style={{ borderColor: 'var(--warn, oklch(0.78 0.18 75))', background: 'var(--bg-1)' }}
                                    >
                                        <div className="flex-1 min-w-0 space-y-0.5">
                                            <div className="font-mono uppercase tracking-wider text-[10px]" style={{ color: 'var(--warn, oklch(0.78 0.18 75))' }}>
                                                Files needing attention · {bundleNotes.length}
                                            </div>
                                            {bundleNotes.map((n, i) => (
                                                <div key={`${i}-${n}`} style={{ color: 'var(--fg-2)' }}>{n}</div>
                                            ))}
                                        </div>
                                        <button
                                            type="button"
                                            onClick={() => setBundleNotes([])}
                                            className="text-[11px] px-2 py-0.5"
                                            style={{ color: 'var(--fg-3)' }}
                                            aria-label="Dismiss the file-notices list"
                                        >
                                            ✕
                                        </button>
                                    </div>
                                )}

                                {/* Queued files */}
                                {queue.length > 0 && (
                                    <div className="rounded border overflow-hidden" style={{ borderColor: 'var(--line-1)' }}>
                                        <div className="flex items-center px-3 py-1.5" style={{ background: 'var(--bg-2)', borderBottom: '1px solid var(--line-1)' }}>
                                            <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                                                Queued · {queue.length} · {humanSize(queueSummary.bytes)}
                                                {queueSummary.unsupported.length > 0 && (
                                                    <span style={{ color: 'var(--warn, oklch(0.78 0.18 75))' }}> · {queueSummary.unsupported.length} unsupported</span>
                                                )}
                                                {queueSummary.oversize.length > 0 && (
                                                    <span style={{ color: 'var(--danger, oklch(0.65 0.2 30))' }}> · {queueSummary.oversize.length} over 6 GB</span>
                                                )}
                                            </div>
                                            <div className="flex-1" />
                                            <button
                                                type="button"
                                                onClick={() => setQueue([])}
                                                className="text-[10px] font-mono uppercase tracking-wider px-2 py-0.5"
                                                style={{ color: 'var(--fg-3)' }}
                                            >
                                                Clear
                                            </button>
                                        </div>
                                        <ul className="max-h-72 overflow-y-auto divide-y" style={{ borderColor: 'var(--line-1)' }}>
                                            {queue.map((q) => {
                                                const oversize = q.size > MAX_FILE_BYTES;
                                                const unsupported = q.category === null;
                                                const canOverrideCrs = supportsCrsOverride(q.category);
                                                const epsg = parseEpsg(q.sourceEpsgText ?? '');
                                                const donated = q.crsDonation;
                                                const usingDonation = donationInEffect(q);
                                                // On a row using a donated coordinate system the
                                                // effective value is that CRS, not an empty box
                                                // asking for a code. Typing one anyway wins: the
                                                // copy is dropped from the ZIP at upload.
                                                const epsgPlaceholder =
                                                    usingDonation && donated ? donated.label : 'EPSG';
                                                const epsgTitle =
                                                    usingDonation && donated
                                                        ? `Using ${donated.label}, copied from ${donated.sourceName}. Type an EPSG code to use that instead — the copy is removed from this upload.`
                                                        : 'Coordinate system to assume when the file declares none — a shapefile with no .prj, or a table of bare eastings and northings. EPSG number only, e.g. 26904. A CRS the file declares always wins.';
                                                return (
                                                    <li key={q.id} className="grid grid-cols-[1fr_140px_84px_70px_auto] items-center gap-2 px-3 py-1.5" style={{ background: 'var(--bg-1)' }}>
                                                        <div className="min-w-0">
                                                            <div className="text-xs truncate" style={{ color: 'var(--fg-0)' }}>{q.name}</div>
                                                            <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                                                                .{q.ext} · {humanSize(q.size)}
                                                                {q.parentZip && <> · <span title={`Extracted from ${q.parentZip}`} style={{ color: 'var(--fg-2)' }}>from {q.parentZip}</span></>}
                                                                {q.status !== 'queued' && <> · <span style={{ color: q.status === 'done' ? 'var(--accent)' : q.status === 'error' ? 'var(--danger, oklch(0.65 0.2 30))' : 'var(--fg-2)' }}>{q.status}</span></>}
                                                                {q.error && <> · <span title={q.error} style={{ color: 'var(--danger, oklch(0.65 0.2 30))' }}>{q.error.slice(0, 40)}</span></>}
                                                                {/* A donated row's hint is the
                                                                    bundler's CRS verdict, which is
                                                                    a sentence — it gets its own
                                                                    full-width line below instead of
                                                                    being cut off at 40 characters. */}
                                                                {!q.error && q.hint && !donated && <> · <span title={q.hint} style={{ color: 'var(--muted-foreground, oklch(0.55 0 0))' }}>{q.hint.slice(0, 40)}{q.hint.length > 40 ? '…' : ''}</span></>}
                                                            </div>
                                                            {/* A row that was given a coordinate
                                                                system reads as resolved and says
                                                                where the CRS came from — the
                                                                bundler's own words while the copy
                                                                is in the ZIP, and this screen's
                                                                when the user has taken it back
                                                                out. It must never go back to
                                                                nagging for an EPSG code it is no
                                                                longer missing. */}
                                                            {donated && (
                                                                <>
                                                                    <div
                                                                        className="text-[10px]"
                                                                        style={{
                                                                            color: usingDonation
                                                                                ? 'var(--fg-2)'
                                                                                : 'var(--warn, oklch(0.78 0.18 75))',
                                                                        }}
                                                                    >
                                                                        {usingDonation
                                                                            ? (q.hint ??
                                                                              `Coordinate system ${donated.label}, copied from ${donated.sourceName}.`)
                                                                            : hasExplicitEpsg(q)
                                                                              ? `EPSG ${epsg.epsg} replaces the coordinate system copied from ${donated.sourceName}: the copy is dropped from this upload, so the code you typed is the one that lands.`
                                                                              : donated.memberName
                                                                                ? `Not using the coordinate system from ${donated.sourceName}. This dataset has no .prj of its own — set an EPSG code on this row, or the ingest will refuse it.`
                                                                                : `Not using the coordinate system from ${donated.sourceName}. This format carries none of its own — set an EPSG code on this row, or its features are stored as 'assumed' with their position uncertain.`}
                                                                    </div>
                                                                    {/* The bundler's verdict is
                                                                        not only about the CRS:
                                                                        most recipients in a real
                                                                        delivery are missing their
                                                                        .dbf as well. While the
                                                                        copy is in the ZIP that
                                                                        verdict IS the line above,
                                                                        but once the user declines
                                                                        it this screen's sentence
                                                                        must be ADDED to the
                                                                        verdict, not substituted
                                                                        for it — otherwise the
                                                                        .dbf warning disappears at
                                                                        exactly the moment the row
                                                                        needs the most attention. */}
                                                                    {!usingDonation && q.hint && (
                                                                        <div
                                                                            className="text-[10px]"
                                                                            style={{
                                                                                color: 'var(--warn, oklch(0.78 0.18 75))',
                                                                            }}
                                                                        >
                                                                            {q.hint}
                                                                        </div>
                                                                    )}
                                                                </>
                                                            )}
                                                            {epsg.error && (
                                                                <div className="text-[10px]" style={{ color: 'var(--danger, oklch(0.65 0.2 30))' }}>
                                                                    {epsg.error}
                                                                </div>
                                                            )}
                                                        </div>
                                                        {unsupported ? (
                                                            <span className="text-[10px] font-mono uppercase tracking-wider px-2 py-0.5 rounded border text-center" style={{ color: 'var(--warn, oklch(0.78 0.18 75))', borderColor: 'var(--warn, oklch(0.78 0.18 75))' }}>
                                                                raster · not supported
                                                            </span>
                                                        ) : (
                                                            <select
                                                                aria-label={`Ingestion category for ${q.file.name}`}
                                                                value={q.category as string}
                                                                onChange={(e) => setCategory(q.id, e.target.value as Category)}
                                                                disabled={q.status === 'uploading' || q.status === 'done'}
                                                                className="text-[11px] px-2 py-1 rounded border"
                                                                style={inputStyle}
                                                            >
                                                                {(Object.keys(CATEGORY_LABEL) as Category[])
                                                                    .filter((cat) => !RETIRED_CATEGORIES.has(cat))
                                                                    .filter((cat) => CATEGORY_EXTS[cat].includes(q.ext))
                                                                    .map((cat) => (
                                                                        <option key={cat} value={cat}>{CATEGORY_LABEL[cat]}</option>
                                                                    ))}
                                                                {/* If no category exactly matches the ext, still allow forcing one --
                                                                    but never a retired one, which the backend would 422. */}
                                                                {(Object.keys(CATEGORY_LABEL) as Category[]).every((cat) => !CATEGORY_EXTS[cat].includes(q.ext)) &&
                                                                    (Object.keys(CATEGORY_LABEL) as Category[])
                                                                        .filter((cat) => !RETIRED_CATEGORIES.has(cat))
                                                                        .map((cat) => (
                                                                            <option key={cat} value={cat}>{CATEGORY_LABEL[cat]}</option>
                                                                        ))
                                                                }
                                                            </select>
                                                        )}
                                                        {/* Per-file CRS override. Only where the
                                                            trigger carries it — an input whose value
                                                            is dropped in transit is worse than none.
                                                            Optional: a file that declares its own
                                                            coordinate system keeps it. */}
                                                        {canOverrideCrs ? (
                                                            <input
                                                                type="text"
                                                                inputMode="numeric"
                                                                value={q.sourceEpsgText ?? ''}
                                                                onChange={(e) => setSourceEpsg(q.id, e.target.value)}
                                                                disabled={q.status === 'uploading' || q.status === 'done'}
                                                                placeholder={epsgPlaceholder}
                                                                title={epsgTitle}
                                                                aria-label={`Source EPSG for ${q.name}`}
                                                                className="text-[11px] font-mono px-2 py-1 rounded border w-full"
                                                                style={{
                                                                    ...inputStyle,
                                                                    borderColor: epsg.error
                                                                        ? 'var(--danger, oklch(0.65 0.2 30))'
                                                                        : 'var(--line-2)',
                                                                }}
                                                            />
                                                        ) : (
                                                            <span />
                                                        )}
                                                        <span className="text-[10px] font-mono uppercase tracking-wider text-center" style={{ color: oversize ? 'var(--danger, oklch(0.65 0.2 30))' : 'var(--fg-3)' }}>
                                                            {oversize ? '>6GB' : ''}
                                                        </span>
                                                        <button
                                                            type="button"
                                                            onClick={() => removeFile(q.id)}
                                                            disabled={q.status === 'uploading'}
                                                            className="text-[11px] px-2 py-0.5"
                                                            style={{ color: 'var(--fg-3)' }}
                                                            aria-label={`Remove ${q.name}`}
                                                        >
                                                            ✕
                                                        </button>
                                                    </li>
                                                );
                                            })}
                                        </ul>
                                    </div>
                                )}
                            </div>
                        )}
                        {step === 'Review' && (
                            <div className="space-y-3 text-xs">
                                {Object.entries(form).map(([k, v]) => (
                                    <div key={k} className="grid grid-cols-[160px_1fr] py-1 border-b" style={{ borderColor: 'var(--line-1)' }}>
                                        <span className="font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>{k}</span>
                                        <span style={{ color: 'var(--fg-0)' }}>{String(v) || '—'}</span>
                                    </div>
                                ))}
                                <div className="grid grid-cols-[160px_1fr] py-1 border-b" style={{ borderColor: 'var(--line-1)' }}>
                                    <span className="font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>initial upload</span>
                                    <span style={{ color: 'var(--fg-0)' }}>
                                        {queueSummary.ok.length === 0
                                            ? 'No files queued — you can add sources later from Corpus → Sources.'
                                            : `${queueSummary.ok.length} file${queueSummary.ok.length === 1 ? '' : 's'} (${humanSize(queueSummary.bytes)}) ready to upload`}
                                        {queueSummary.unsupported.length > 0 && (
                                            <span style={{ color: 'var(--warn, oklch(0.78 0.18 75))' }}>
                                                {' · '}{queueSummary.unsupported.length} unsupported will be skipped
                                            </span>
                                        )}
                                        {queueSummary.oversize.length > 0 && (
                                            <span style={{ color: 'var(--danger, oklch(0.65 0.2 30))' }}>
                                                {' · '}{queueSummary.oversize.length} over 6 GB will be skipped
                                            </span>
                                        )}
                                        {queueSummary.badEpsg.length > 0 && (
                                            <span style={{ color: 'var(--danger, oklch(0.65 0.2 30))' }}>
                                                {' · '}{queueSummary.badEpsg.length} invalid EPSG code
                                                {queueSummary.badEpsg.length === 1 ? '' : 's'} — fix in Corpus before creating
                                            </span>
                                        )}
                                    </span>
                                </div>
                                {submitProgress && (
                                    <div className="mt-2 text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-2)' }}>
                                        Uploading {submitProgress.done} / {submitProgress.total}
                                        <div className="h-1 mt-1 rounded" style={{ background: 'var(--bg-2)' }}>
                                            <div
                                                className="h-full rounded"
                                                style={{
                                                    width: submitProgress.total === 0 ? '100%' : `${(submitProgress.done / submitProgress.total) * 100}%`,
                                                    background: 'var(--accent)',
                                                    transition: 'width 120ms linear',
                                                }}
                                            />
                                        </div>
                                    </div>
                                )}
                            </div>
                        )}
                    </Card>

                    <footer className="flex justify-between mt-4">
                        <button type="button" onClick={back} disabled={stepIdx === 0 || submitting} className="text-[10px] font-mono uppercase tracking-wider px-3 py-1.5 rounded border disabled:opacity-30" style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}>
                            ← Back
                        </button>
                        {step !== 'Review' ? (
                            <button type="button" onClick={next} className="text-[10px] font-mono uppercase tracking-wider px-3 py-1.5 rounded border" style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}>
                                Next →
                            </button>
                        ) : (
                            <button
                                type="button"
                                onClick={submit}
                                disabled={submitting || !form.name || queueSummary.badEpsg.length > 0}
                                className="text-[10px] font-mono uppercase tracking-wider px-3 py-1.5 rounded border disabled:opacity-40"
                                style={{ color: 'var(--bg-0)', background: 'var(--accent)', borderColor: 'var(--accent-dim)' }}
                            >
                                {submitting
                                    ? (submitProgress ? `Uploading ${submitProgress.done}/${submitProgress.total}…` : 'Creating…')
                                    : queueSummary.ok.length > 0
                                        ? `Create project + upload ${queueSummary.ok.length} file${queueSummary.ok.length === 1 ? '' : 's'} →`
                                        : 'Create project →'}
                            </button>
                        )}
                    </footer>
                    {submitError && (
                        <div className="mt-3 text-[11px]" style={{ color: 'var(--danger, oklch(0.65 0.2 30))' }}>
                            {submitError}
                        </div>
                    )}
                </div>
            </div>
        </AppLayout>
    );
}

const inputStyle = { background: 'var(--bg-2)', color: 'var(--fg-0)', borderColor: 'var(--line-2)' } as React.CSSProperties;

function Field({ label, required, children }: { label: string; required?: boolean; children: React.ReactNode }) {
    return (
        <label className="block">
            <span className="text-[10px] font-mono uppercase tracking-wider mb-1 block" style={{ color: 'var(--fg-3)' }}>
                {label}{required && <span style={{ color: 'var(--accent)' }}> *</span>}
            </span>
            {children}
        </label>
    );
}
