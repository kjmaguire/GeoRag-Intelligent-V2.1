import { useEffect, useRef, useState } from 'react';
import { filesFromDataTransfer } from '@/lib/dropFiles';
import { Head, Link, router } from '@inertiajs/react';
import JSZip from 'jszip';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill } from '@/Components/Foundry/primitives';
import {
    acceptedExtensions,
    categoryForExtension,
    parseEpsg,
    supportsCrsOverride,
    type Category,
} from '@/lib/uploadCategories';
import {
    BUNDLE_MEMBER_EXTS,
    bundleKey,
    dedupeFiles,
    fileKey,
    groupShapefiles,
    type CrsProvenance,
} from '@/lib/shapefileBundle';

/**
 * Foundry / DataImportWizard
 *
 * Real upload + redirect-to-runs.
 *
 * Step 1 ("Drop") — user picks a target project (fetched from /api/v1/projects)
 *                   and drops one or more files.
 * Step 2 ("Submit") — files are POSTed to /api/v1/projects/{id}/upload.
 *                     When EVERY file queues successfully we router.visit to
 *                     the project's IngestionRuns page, which is the canonical
 *                     surface for watching ingest progress (Echo + 5 s poll
 *                     fallback). On partial failure we stay here so the
 *                     per-file failure pills remain visible, and show a
 *                     "N queued · M failed" summary with a link to the runs
 *                     page.
 *
 * The previous incarnation of this page synthesized progress with a
 * setInterval ticker — that was a UX mockup unconnected to any real
 * pipeline. Deleted as part of the Phase 2b real-time staleness fix:
 * the only place real ingest progress lives is /projects/{slug}/ingestion-runs.
 */

interface ProjectPick {
    project_id: string;
    slug: string;
    project_name: string;
    region: string | null;
    commodity: string | null;
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
 * whole selection with the control above the list.
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
 * NewProject.tsx carries a copy of this function verbatim. The two screens
 * have drifted before and it caused real bugs; if this wording changes, both
 * copies change together.
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

/** A queued file with a stable per-entry id so duplicate filenames never
 *  collide when matching upload outcomes back to rows. */
interface QueuedFile {
    id: string;
    file: File;
    /**
     * Identity that survives re-grouping — `bundleKey`/`fileKey`.
     *
     * Every added batch re-groups the whole selection, which re-zips each
     * bundle and so replaces its `File`. This carries a row's category and
     * EPSG edits across that rebuild, and stops an uploaded row being
     * queued a second time.
     */
    selectionKey?: string;
    /**
     * The selected files behind this row — a bundle's members, or the file
     * itself. Read when the row is removed, so its sources leave the
     * accumulated selection with it rather than reappearing on the next add.
     */
    sources?: File[];
    /**
     * Category to upload under, when the extension alone would get it wrong.
     *
     * A shapefile bundle is the case that matters: groupShapefiles() names it
     * `<stem>.zip`, and categoryForExtension('zip') answers `archive`, which
     * routes the bundle to ingest_zip_archive — a workflow with no branch for
     * .shp/.shx/.dbf/.prj, so it counted all four members as "unknown",
     * wrote nothing, and reported the run completed. Carry the intended
     * category with the file instead of re-deriving it from a name we chose.
     */
    category?: Category;
    /**
     * CRS the user asserts for this file, as typed. Integer EPSG only.
     *
     * Rides the same struct as `category` above, for the same reason: an
     * override re-derived at submit time is an override that silently applies
     * to the wrong row the moment the queue is reordered or filtered.
     *
     * It is a HINT, not a command. A file that declares its own coordinate
     * system keeps it; this fills the gap left by a missing `.prj`, or by a
     * drill table of bare eastings and northings that the tabular ingest has
     * until now silently assumed was UTM 13N. The server re-measures the
     * geometry against the claimed CRS rather than taking the human's word
     * for it.
     */
    sourceEpsgText?: string;
    /**
     * Verdict from the bundler — an incomplete shapefile or MapInfo set.
     * Advisory: the file still uploads. Never an error, and never rendered
     * as one.
     */
    bundleNote?: string;
    /**
     * Set when this bundle had no `.prj` and was given a copy of the one
     * coordinate system the selection agreed on.
     *
     * Not a note: it changes what is inside the ZIP, so it is also what
     * `uploadOne` reads to decide whether to strip that copy back out.
     */
    crsDonation?: DonatedCrs;
}

interface UploadOutcome {
    /** QueuedFile.id — the join key back to the row (NOT the filename). */
    id: string;
    ok: boolean;
    message: string;
}

// The drop zone gate. #144 moved uploadOne() onto the shared category map but
// left this list behind, so the wizard still refused drill, Excel, GIS and LAS
// files at intake - the API accepted them, but nothing got that far. Derive it
// from the same source as everything else.
const ACCEPTED_EXTENSIONS = acceptedExtensions();

/**
 * What the OS file dialog is allowed to show.
 *
 * The category map alone is NOT enough, and that gap is the root of the
 * SRID-4326 corruption rather than a cosmetic annoyance. No sidecar has an
 * upload category — `.shx`, `.dbf`, `.prj`, `.cpg`, and MapInfo's
 * `.dat`/`.map`/`.id` are bundle members by definition — so an `accept=`
 * built from `acceptedExtensions()` greyed every one of them out. The picker
 * therefore handed groupShapefiles a lone `.shp`, groupShapefiles zipped a
 * bundle with no `.prj`, and the parser filed a UTM shapefile as SRID 4326
 * at longitude 400,797. Drag-and-drop never had the problem; the picker did,
 * on every shapefile anyone chose through it.
 */
const PICKER_EXTENSIONS = [...new Set([...ACCEPTED_EXTENSIONS, ...BUNDLE_MEMBER_EXTS])].sort();

let nextQueueId = 0;
function newQueueId(): string {
    nextQueueId += 1;
    return `qf-${Date.now()}-${nextQueueId}`;
}

function fileExtension(name: string): string {
    return (name.split('.').pop() ?? '').toLowerCase();
}

/**
 * Why a given format was not imported, in the user's terms.
 *
 * "Unsupported file type" is true and useless — it does not distinguish a
 * format nobody has written a parser for from a file that is simply a
 * companion of one already queued. A geologist looking at a refused
 * `.rdtmm` needs to know it is an inversion output with no reader, not that
 * they picked the wrong button.
 */
const REJECTION_REASONS: Record<string, string> = {
    aux: 'raster auxiliary file — companion of the .tif, not data',
    ovr: 'raster overview file — companion of the .tif, not data',
    mdb: 'Access database — no reader yet; export the tables to CSV to import them',
    str: 'Surpac string file — no reader yet',
    rdtmm: 'UBC-GIF / RDTM inversion output — no reader yet',
    rdtmp: 'UBC-GIF / RDTM inversion output — no reader yet',
    rdtmd: 'UBC-GIF / RDTM inversion output — no reader yet',
    inp: 'inversion input deck — no reader yet',
    chg: 'inversion control file — no reader yet',
    jpg: 'photo — images are not ingested as documents',
    jpeg: 'photo — images are not ingested as documents',
    png: 'image — not ingested as a document',
};

function rejectionReason(ext: string): string {
    if (REJECTION_REASONS[ext]) return REJECTION_REASONS[ext];
    // dcinv2d.011 / ipinv2d.016 and friends: the "extension" is an iteration
    // number, which is the tell that this is a solver's numbered output.
    if (/^\d+$/.test(ext)) return 'numbered solver output — no reader yet';
    return 'no reader for this format yet';
}

/** Refused files grouped by extension, each group carrying its reason. */
function groupByExtension(names: string[]): { ext: string; names: string[]; reason: string }[] {
    const byExt = new Map<string, string[]>();
    for (const n of names) {
        const ext = fileExtension(n) || '(none)';
        const list = byExt.get(ext);
        if (list) {
            list.push(n);
        } else {
            byExt.set(ext, [n]);
        }
    }
    return [...byExt.entries()]
        .sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]))
        .map(([ext, group]) => ({ ext, names: group, reason: rejectionReason(ext) }));
}

export default function FoundryDataImportWizard() {
    const [projects, setProjects] = useState<ProjectPick[] | null>(null);
    const [projectsError, setProjectsError] = useState<string | null>(null);
    const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null);
    const [files, setFiles] = useState<QueuedFile[]>([]);
    /**
     * Every file the intake refused, in full.
     *
     * Was a single pre-formatted string that named `rejected.slice(0, 3)`. On
     * the delivery this was written against that meant 18 refusals rendered as
     * three filenames and a count — the whole Centennial geophysics folder
     * (an Access database plus seven inversion outputs) was represented by one
     * name the user could not act on. A refusal you cannot enumerate is a
     * refusal you cannot fix, so the list is kept whole and grouped for
     * reading rather than truncated for tidiness.
     */
    const [rejected, setRejected] = useState<string[]>([]);
    /** Incomplete-set verdicts from the bundler, and orphaned members kept
     *  with the reason they could not be attached to anything. */
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
    const [dragging, setDragging] = useState(false);
    const [submitting, setSubmitting] = useState(false);
    const [outcomes, setOutcomes] = useState<UploadOutcome[]>([]);
    const [finished, setFinished] = useState(false);
    /**
     * Every file selected so far, across every drop and pick.
     *
     * A ref, not state: `addFiles` reads AND writes it within one call, so
     * a state value captured by the closure would be a batch behind — the
     * staleness that makes a second drop group in isolation.
     */
    const selectedFilesRef = useRef<File[]>([]);
    /** Live queue and outcomes, for reading during a regroup. */
    const filesRef = useRef<QueuedFile[]>([]);
    filesRef.current = files;
    const outcomesRef = useRef<UploadOutcome[]>([]);
    outcomesRef.current = outcomes;
    const fileInputRef = useRef<HTMLInputElement | null>(null);
    const folderInputRef = useRef<HTMLInputElement | null>(null);

    const selectedProject = projects?.find((p) => p.project_id === selectedProjectId) ?? null;

    // Pull the user's projects on mount so the picker has real data.
    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const res = await fetch('/api/v1/projects', {
                    credentials: 'same-origin',
                    headers: { Accept: 'application/json' },
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const body = await res.json();
                // ProjectController::index returns either {data: [...]} (api
                // resource) or a bare array depending on resource wrapping;
                // accept both shapes defensively.
                const list = Array.isArray(body) ? body : (body.data ?? []);
                const mapped: ProjectPick[] = list.map((p: Record<string, unknown>) => ({
                    project_id: String(p.project_id ?? p.id ?? ''),
                    slug: String(p.slug ?? ''),
                    project_name: String(p.project_name ?? p.name ?? '(unnamed project)'),
                    region: (p.region as string | null) ?? null,
                    commodity: (p.commodity as string | null) ?? null,
                }));
                if (!cancelled) {
                    setProjects(mapped);
                    if (mapped.length === 1) setSelectedProjectId(mapped[0].project_id);
                }
            } catch (err) {
                if (!cancelled) {
                    setProjectsError(err instanceof Error ? err.message : String(err));
                }
            }
        })();
        return () => {
            cancelled = true;
        };
    }, []);

    // Shared intake path for the picker AND the drop zone: filter to the
    // accepted extensions up front and surface anything rejected as an
    // inline message instead of letting the server 422 it later.
    async function addFiles(incoming: FileList | File[] | null) {
        if (!incoming) return;
        const incomingArr = Array.from(incoming);
        if (incomingArr.length === 0) return;
        const accepted: QueuedFile[] = [];
        const rejected: string[] = [];

        // Group the WHOLE accumulated selection, not just this batch. A
        // `.dbf` added in a second drop has no `.shp` in ITS batch, so
        // per-batch grouping treated it as a standalone dBASE table and
        // uploaded it alone — measured on the 2026-08-24 delivery, where
        // seven of eight bundles reached storage holding only `.shp` +
        // `.prj` while four loose `.dbf` files went up beside them.
        const arr = dedupeFiles([...selectedFilesRef.current, ...incomingArr]);
        selectedFilesRef.current = arr;

        // Per-row edits and already-settled rows, carried across the
        // rebuild — re-grouping re-zips every bundle, so `File` identity
        // does not survive it.
        const edits = new Map<string, { category?: Category; sourceEpsgText?: string }>();
        const settled = new Set<string>();
        const settledIds = new Set(outcomesRef.current.filter((o) => o.ok).map((o) => o.id));
        for (const qf of filesRef.current) {
            if (qf.selectionKey === undefined) continue;
            edits.set(qf.selectionKey, {
                category: qf.category,
                sourceEpsgText: qf.sourceEpsgText,
            });
            if (settledIds.has(qf.id)) settled.add(qf.selectionKey);
        }

        // Zip each .shp back together with its .shx/.dbf/.prj siblings before
        // anything else looks at the list. Uploaded alone, a .shp cannot be
        // parsed; the sidecars have no category of their own and would
        // otherwise be rejected here as unsupported.
        // Falling back to the raw list keeps a zip failure (out of memory on a
        // very large .dbf, say) from swallowing the whole selection: the .shp
        // is queued and fails server-side with a message, which beats a drop
        // zone that silently does nothing.
        const { bundles, passthrough, unusable, wktRecipients } = await groupShapefiles(
            arr,
        ).catch(() => ({
            bundles: [],
            passthrough: arr,
            unusable: [],
            crsDonation: null,
            wktRecipients: [],
        }));
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
            accepted.push({
                id: newQueueId(),
                file: b.file,
                selectionKey: key,
                sources: b.sources,
                category: prior?.category ?? 'spatial',
                sourceEpsgText: prior?.sourceEpsgText,
                bundleNote: b.verdict ?? undefined,
                crsDonation: donated,
            });
            // A set that was given its coordinate system is not an incomplete
            // one, and listing seven of them under a heading that asks for
            // missing files is the noise this change exists to remove: it is
            // reported on its own line above instead. Anything ELSE the set
            // is missing (a .dbf) still belongs here — `missing` has already
            // had `prj` removed for a recipient, and a missing `.shx` has
            // never been worth a word.
            const stillIncomplete = !donated || b.missing.some((e) => e !== 'shx');
            if (b.verdict && stillIncomplete) notes.push(`${b.stem}: ${b.verdict}`);
        }
        // An orphaned sidecar is NOT lumped in with "unsupported file type".
        // It is a supported format whose master was not selected, and saying
        // which master is missing is the difference between a user fixing the
        // selection and a user concluding the drop zone is broken.
        for (const u of unusable) notes.push(`${u.file.name}: ${u.reason}`);

        // Matched by File identity, never by name: two folders in one drop
        // can each hold a `plan.dxf`, and only the object the grouper saw
        // beside the donor is the recipient.
        const wktCrsByFile = new Map<File, DonatedCrs>(
            wktRecipients.map((r): [File, DonatedCrs] => [r.file, r.crs]),
        );
        for (const f of passthrough) {
            const key = fileKey(f);
            if (settled.has(key)) continue;
            if (ACCEPTED_EXTENSIONS.includes(fileExtension(f.name))) {
                const prior = edits.get(key);
                accepted.push({
                    id: newQueueId(),
                    file: f,
                    selectionKey: key,
                    sources: [f],
                    category: prior?.category,
                    sourceEpsgText: prior?.sourceEpsgText,
                    crsDonation: wktCrsByFile.get(f),
                });
            } else {
                rejected.push(f.name);
            }
        }
        // Replaced, not appended: `accepted` is the whole selection
        // re-grouped, so appending would queue every earlier file again.
        // Rows that already uploaded successfully keep their existing entry
        // (`settled` above skipped rebuilding them) so the no-re-upload
        // guard in handleSubmit still recognises them.
        setFiles((prev) => [
            ...prev.filter((qf) => settledIds.has(qf.id)),
            ...accepted,
        ]);
        setRejected(rejected);
        setBundleNotes(notes);
        // Keep successful outcomes (their pills + the no-re-upload guard in
        // handleSubmit depend on them); clear stale failures so the new
        // attempt starts clean.
        setOutcomes((prev) => prev.filter((o) => o.ok));
        setFinished(false);
    }

    function removeFile(id: string) {
        // Un-select the row's SOURCE files too, or the next added file
        // re-groups the whole selection and the removed row comes back.
        const target = filesRef.current.find((qf) => qf.id === id);
        if (target) {
            const gone = new Set((target.sources ?? [target.file]).map(fileKey));
            selectedFilesRef.current = selectedFilesRef.current.filter(
                (f) => !gone.has(fileKey(f)),
            );
        }
        setFiles((prev) => prev.filter((qf) => qf.id !== id));
        setOutcomes((prev) => prev.filter((o) => o.id !== id && o.ok));
        setFinished(false);
    }

    function setSourceEpsg(id: string, text: string) {
        setFiles((prev) => prev.map((qf) => (qf.id === id ? { ...qf, sourceEpsgText: text } : qf)));
    }

    /** The category this row will actually be POSTed under. */
    function effectiveCategory(qf: QueuedFile): Category | null {
        return qf.category ?? categoryForExtension(fileExtension(qf.file.name));
    }

    // An EPSG the API would refuse blocks the submit rather than being
    // dropped on the way out. A control whose value is silently discarded is
    // the failure this whole change set exists to stop.
    const epsgErrorCount = files.filter(
        (qf) =>
            supportsCrsOverride(effectiveCategory(qf)) &&
            parseEpsg(qf.sourceEpsgText ?? '').error !== undefined,
    ).length;

    /**
     * True when this row carries an EPSG code the upload will actually send.
     *
     * On a row that was given a donated `.prj`, that code is only obeyed if
     * the copy is removed first — the file's own declaration outranks
     * `source_epsg` server-side — so this is also the test for stripping it.
     */
    function hasExplicitEpsg(qf: QueuedFile): boolean {
        return (
            supportsCrsOverride(effectiveCategory(qf)) &&
            parseEpsg(qf.sourceEpsgText ?? '').epsg !== undefined
        );
    }

    /** True when the copied coordinate system is what this row will upload with. */
    function donationInEffect(qf: QueuedFile): boolean {
        return qf.crsDonation !== undefined && donateCrs && !hasExplicitEpsg(qf);
    }

    const donationRows = files.filter((qf) => qf.crsDonation !== undefined);
    const donationOverrides = donationRows.filter(hasExplicitEpsg).length;
    // WKT-carriage recipients (lone .dxf/.dgn) among the rows the donation
    // actually reaches: the banner's toggle-off consequence differs — a CAD
    // file is never refused, it lands as 'assumed'.
    const donationWktUsing = donationRows.filter(
        (qf) => !hasExplicitEpsg(qf) && qf.crsDonation?.memberName === undefined,
    ).length;
    const donation =
        donationRows.length > 0
            ? donationSummary(
                  donationRows.map((qf) => qf.crsDonation as DonatedCrs),
                  donationOverrides,
                  donateCrs,
                  donationWktUsing,
              )
            : null;

    /**
     * A dropped folder must yield the files inside it, at any depth.
     *
     * This used to read `e.dataTransfer.files` alone. A browser puts a dropped
     * FOLDER into that list as one 0-byte File named after the directory, so
     * dropping five folders produced five rows that failed the extension check
     * and were reported as "unsupported files" — while every real file stayed
     * on disk. Measured against a 72-file delivery: zero uploads.
     */
    async function onDrop(e: React.DragEvent<HTMLDivElement>) {
        e.preventDefault();
        setDragging(false);
        if (submitting) return;
        // Snapshot the plain file list NOW. `filesFromDataTransfer` reads the
        // entry list synchronously before it awaits, but this fallback runs
        // after that await, by which point `e.dataTransfer` may already be
        // detached — reading it there would turn a recoverable drop into an
        // empty one.
        const plain = e.dataTransfer?.files ? Array.from(e.dataTransfer.files) : [];
        const collected = await filesFromDataTransfer(e.dataTransfer);
        addFiles(collected.length > 0 ? collected : plain);
    }

    function csrfHeader(): Record<string, string> {
        const token =
            document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') ?? null;
        return token ? { 'X-CSRF-TOKEN': token } : {};
    }

    async function uploadOne(projectId: string, qf: QueuedFile): Promise<UploadOutcome> {
        // UploadController requires `category` — omitting it 422'd EVERY
        // wizard upload while the create-project flow (which sends it)
        // worked, so the gap went unnoticed. Derived from the extension via
        // the shared map, which is what keeps this screen and the picker in
        // NewProject agreeing with the backend. This wizard previously
        // hardcoded PDF/TIFF/ZIP and refused drill and GIS files client-side
        // even after the API began accepting them.
        const ext = fileExtension(qf.file.name);
        const category = qf.category ?? categoryForExtension(ext);
        if (!category) {
            return {
                id: qf.id,
                ok: false,
                message:
                    `Unsupported type .${ext} — accepted: ` +
                    acceptedExtensions().join(', '),
            };
        }
        // `source_epsg`, an integer, and only when the user typed a legal one
        // for a category whose trigger carries it. Same name and same type as
        // the field the tabular ingest has always taken, so the two paths
        // that share this screen cannot end up with two names for one idea.
        const epsg = parseEpsg(qf.sourceEpsgText ?? '');
        const explicitEpsg = supportsCrsOverride(category) && epsg.epsg !== undefined;

        // The donated `.prj` is taken back out of the archive when the user
        // turned the donation off, or when they typed a code for this row.
        // Both have to change the bytes, not just the text: a CRS the file
        // declares always beats `source_epsg`, so a copy left in the ZIP
        // would outrank the code the user typed and the override would look
        // accepted while doing nothing.
        let payload = qf.file;
        const donatedMember = qf.crsDonation?.memberName;
        if (donatedMember && (!donateCrs || explicitEpsg)) {
            try {
                payload = await withoutDonatedPrj(qf.file, donatedMember);
            } catch (err) {
                // Uploading the donated copy anyway would be uploading the
                // coordinate system the user just declined.
                return {
                    id: qf.id,
                    ok: false,
                    message:
                        `Could not rebuild ${qf.file.name} without the copied ` +
                        `${donatedMember}: ` +
                        (err instanceof Error ? err.message : String(err)),
                };
            }
        }

        const fd = new FormData();
        fd.append('file', payload);
        fd.append('category', category);
        if (explicitEpsg && epsg.epsg !== undefined) {
            fd.append('source_epsg', String(epsg.epsg));
        }
        // A WKT-carriage recipient (a lone .dxf/.dgn — no ZIP to hold a
        // copied .prj) sends the donation as text; the server resolves it
        // with pyproj. Declining the donation or typing a code means simply
        // not sending it — there are no bytes to rebuild.
        if (qf.crsDonation?.wkt && donationInEffect(qf)) {
            fd.append('source_crs_wkt', qf.crsDonation.wkt);
        }
        try {
            const res = await fetch(`/api/v1/projects/${projectId}/upload`, {
                method: 'POST',
                credentials: 'same-origin',
                headers: { Accept: 'application/json', ...csrfHeader() },
                body: fd,
            });
            if (!res.ok) {
                const body = await res.json().catch(() => ({}));
                return {
                    id: qf.id,
                    ok: false,
                    message: body.message ?? `HTTP ${res.status}`,
                };
            }
            return { id: qf.id, ok: true, message: 'queued' };
        } catch (err) {
            return {
                id: qf.id,
                ok: false,
                message: err instanceof Error ? err.message : String(err),
            };
        }
    }

    async function handleSubmit() {
        if (!selectedProject || files.length === 0) return;
        setSubmitting(true);
        setFinished(false);

        // On a retry after partial failure, files that already queued keep
        // their outcome and are NOT re-uploaded (re-POSTing would ingest
        // them twice). Only never-attempted / failed files go out again.
        const priorOk = outcomes.filter((o) => o.ok);
        const priorOkIds = new Set(priorOk.map((o) => o.id));
        const pending = files.filter((qf) => !priorOkIds.has(qf.id));
        setOutcomes(priorOk);

        // Bounded-concurrency pool (3 wide): per-file requests stay
        // independent so failures still surface per file, but a 20-file
        // import no longer serialises 20 round-trips end-to-end. Results
        // land in input order for stable UI rows.
        const CONCURRENCY = 3;
        const results: UploadOutcome[] = new Array(pending.length);
        let nextIndex = 0;
        async function worker() {
            for (;;) {
                const i = nextIndex++;
                if (i >= pending.length) return;
                results[i] = await uploadOne(selectedProject!.project_id, pending[i]);
                setOutcomes([...priorOk, ...(results.filter(Boolean) as UploadOutcome[])]);
            }
        }
        await Promise.all(
            Array.from({ length: Math.min(CONCURRENCY, pending.length) }, () => worker()),
        );

        setSubmitting(false);
        setFinished(true);

        // Navigate ONLY when every file queued. On partial failure we stay
        // put so the per-file failure pills survive — auto-navigating threw
        // that context away and the user never learned which files failed.
        const allOk = results.every((r) => r.ok);
        if (allOk) {
            // Hand off to the page that actually shows live ingest progress.
            router.visit(`/projects/${selectedProject.slug}/ingestion-runs`);
        }
    }

    const queuedCount = outcomes.filter((o) => o.ok).length;
    const failedCount = outcomes.filter((o) => !o.ok).length;

    return (
        <AppLayout>
            <Head title="Data import — GeoRAG" />

            <div
                className="flex-1 overflow-y-auto"
                style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}
            >
                <PageHeader
                    eyebrow="DATA · IMPORT"
                    title="Upload files for ingestion"
                    sub="Pick a project, drop files, and watch them ingest on the Ingestion Runs page."
                />

                <div className="max-w-3xl mx-auto px-8 py-6 space-y-5">
                    <Card eyebrow="STEP 1" title="Target project">
                        {projectsError && (
                            <div
                                className="text-xs mb-3 px-3 py-2 rounded border"
                                style={{
                                    borderColor: 'rgba(220, 38, 38, 0.4)',
                                    background: 'rgba(127, 29, 29, 0.15)',
                                    color: '#fca5a5',
                                }}
                            >
                                Couldn't load projects: {projectsError}. Try refreshing.
                            </div>
                        )}
                        {projects === null && !projectsError && (
                            <div className="text-xs" style={{ color: 'var(--fg-3)' }}>
                                Loading projects…
                            </div>
                        )}
                        {projects !== null && projects.length === 0 && (
                            <div className="text-xs" style={{ color: 'var(--fg-3)' }}>
                                You don't have any projects yet. Create one at{' '}
                                <a
                                    href="/foundry/projects/new"
                                    style={{ color: 'var(--accent)' }}
                                    className="underline"
                                >
                                    /foundry/projects/new
                                </a>{' '}
                                before uploading.
                            </div>
                        )}
                        {projects !== null && projects.length > 0 && (
                            <select
                                aria-label="Target project"
                                value={selectedProjectId ?? ''}
                                onChange={(e) => setSelectedProjectId(e.target.value || null)}
                                className="w-full text-sm px-3 py-2 rounded border"
                                style={{
                                    background: 'var(--bg-2)',
                                    borderColor: 'var(--line-2)',
                                    color: 'var(--fg-0)',
                                }}
                            >
                                <option value="">Pick a project…</option>
                                {projects.map((p) => (
                                    <option key={p.project_id} value={p.project_id}>
                                        {p.project_name}
                                        {p.region ? ` · ${p.region}` : ''}
                                        {p.commodity ? ` · ${p.commodity}` : ''}
                                    </option>
                                ))}
                            </select>
                        )}
                    </Card>

                    <Card eyebrow="STEP 2" title="Drop files">
                        <div
                            onDragOver={(e) => {
                                e.preventDefault();
                                setDragging(true);
                            }}
                            onDragLeave={() => setDragging(false)}
                            onDrop={onDrop}
                            onClick={() => fileInputRef.current?.click()}
                            role="button"
                            tabIndex={0}
                            onKeyDown={(e) => {
                                if (e.key === 'Enter' || e.key === ' ') fileInputRef.current?.click();
                            }}
                            className="h-44 rounded-md border-2 border-dashed flex flex-col items-center justify-center cursor-pointer transition-colors"
                            style={{
                                borderColor: dragging ? 'var(--accent)' : 'var(--line-2)',
                                background: dragging ? 'var(--accent-bg)' : 'var(--bg-2)',
                            }}
                        >
                            <div className="text-sm font-medium mb-1" style={{ color: 'var(--fg-1)' }}>
                                Drop reports, drill data or GIS files here
                            </div>
                            <div
                                className="text-[11px] font-mono uppercase tracking-wider mb-3"
                                style={{ color: 'var(--fg-3)' }}
                            >
                                or use the file picker
                            </div>
                            {/* `accept` is derived from the same map as
                                everything else on this screen. #144 rewired
                                intake and uploadOne() onto the shared category
                                map but left this attribute at the old three
                                formats — so the OS dialog greyed out .csv,
                                .xlsx, .las, .gpkg, .geojson and .qgz, four days
                                after the team shipped three workflows
                                specifically to accept them. Drag-and-drop
                                worked; the picker did not, and the label said
                                the same wrong thing.

                                It stayed half wrong afterwards: bundle members
                                have no category, so .shx/.dbf/.prj/.cpg and
                                the MapInfo sidecars were STILL greyed out and
                                the picker still handed the bundler a lone
                                .shp — a bundle with no .prj, which is exactly
                                the input that filed a UTM shapefile at SRID
                                4326. PICKER_EXTENSIONS adds them back. */}
                            <input
                                ref={fileInputRef}
                                type="file"
                                multiple
                                accept={PICKER_EXTENSIONS.map((e) => `.${e}`).join(',')}
                                className="hidden"
                                onChange={(e) => {
                                    addFiles(e.target.files);
                                    // Reset so re-picking the same file fires change again.
                                    e.target.value = '';
                                }}
                            />
                            {/* Folder picking needs its own input. `webkitdirectory`
                                cannot be toggled on one input alongside `accept` —
                                the attribute is set through a ref callback because
                                React does not render it — and a directory pick
                                deliberately carries NO `accept`: the OS dialog
                                greys out whole folders when it is present, which
                                is the same trap that once hid every sidecar. Files
                                are filtered after selection instead, so the picker
                                shows the folder and the manifest explains what was
                                skipped. */}
                            <input
                                ref={(node) => {
                                    folderInputRef.current = node;
                                    if (node) {
                                        node.setAttribute('webkitdirectory', '');
                                        node.setAttribute('directory', '');
                                    }
                                }}
                                type="file"
                                multiple
                                className="hidden"
                                onChange={(e) => {
                                    addFiles(e.target.files);
                                    e.target.value = '';
                                }}
                            />
                            <button
                                type="button"
                                onClick={(e) => {
                                    e.stopPropagation();
                                    folderInputRef.current?.click();
                                }}
                                className="text-[10px] font-mono uppercase tracking-wider px-3 py-1.5 rounded border mr-2"
                                style={{
                                    color: 'var(--fg-1)',
                                    background: 'var(--bg-2)',
                                    borderColor: 'var(--line-2)',
                                }}
                            >
                                Pick folder →
                            </button>
                            <button
                                type="button"
                                onClick={(e) => {
                                    e.stopPropagation();
                                    fileInputRef.current?.click();
                                }}
                                className="text-[10px] font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                style={{
                                    color: 'var(--accent)',
                                    background: 'var(--accent-bg)',
                                    borderColor: 'var(--accent-dim)',
                                }}
                            >
                                Pick files →
                            </button>
                        </div>

                        {rejected.length > 0 && (
                            <div
                                className="mt-3 text-xs px-3 py-2 rounded border"
                                style={{
                                    color: 'var(--warn)',
                                    borderColor: 'var(--warn)',
                                    background: 'color-mix(in oklch, var(--warn) 10%, transparent)',
                                }}
                            >
                                <div className="font-mono uppercase tracking-wider text-[10px] mb-1">
                                    Not imported — {rejected.length} file
                                    {rejected.length === 1 ? '' : 's'}
                                </div>
                                {groupByExtension(rejected).map((g) => (
                                    <div key={g.ext} className="mt-1">
                                        <span className="font-mono">.{g.ext}</span>{' '}
                                        <span style={{ color: 'var(--fg-2)' }}>
                                            ({g.names.length}) — {g.reason}
                                        </span>
                                        <ul className="ml-4 mt-0.5" style={{ color: 'var(--fg-2)' }}>
                                            {g.names.map((n) => (
                                                <li key={n} className="font-mono text-[11px]">
                                                    {n}
                                                </li>
                                            ))}
                                        </ul>
                                    </div>
                                ))}
                                <div className="mt-2" style={{ color: 'var(--fg-2)' }}>
                                    Accepted types: {ACCEPTED_EXTENSIONS.join(', ')}.
                                </div>
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
                                className="mt-3 text-xs px-3 py-2 rounded border"
                                style={{
                                    color: 'var(--fg-1)',
                                    borderColor: donateCrs ? 'var(--accent-dim)' : 'var(--warn)',
                                    background: donateCrs
                                        ? 'var(--accent-bg)'
                                        : 'color-mix(in oklch, var(--warn) 8%, transparent)',
                                }}
                            >
                                <div
                                    className="text-[10px] font-mono uppercase tracking-[0.12em]"
                                    style={{ color: donateCrs ? 'var(--accent)' : 'var(--warn)' }}
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
                            Deliberately NOT folded into the "unsupported file"
                            line above: these are supported formats, and the
                            fix is to re-drop the folder, not to give up on the
                            format. Nothing here was discarded — the bundles
                            still upload. */}
                        {bundleNotes.length > 0 && (
                            <div
                                className="mt-3 text-xs px-3 py-2 rounded border space-y-1"
                                style={{
                                    color: 'var(--fg-1)',
                                    borderColor: 'var(--warn)',
                                    background: 'color-mix(in oklch, var(--warn) 8%, transparent)',
                                }}
                            >
                                <div
                                    className="text-[10px] font-mono uppercase tracking-[0.12em]"
                                    style={{ color: 'var(--warn)' }}
                                >
                                    Files needing attention · {bundleNotes.length}
                                </div>
                                {bundleNotes.map((n, i) => (
                                    <div key={`${i}-${n}`} style={{ color: 'var(--fg-2)' }}>
                                        {n}
                                    </div>
                                ))}
                            </div>
                        )}

                        {files.length > 0 && (
                            <div className="mt-4">
                                <div
                                    className="text-[10px] font-mono uppercase tracking-[0.12em] mb-2"
                                    style={{ color: 'var(--fg-3)' }}
                                >
                                    Selected · {files.length}
                                </div>
                                {files.some((qf) => supportsCrsOverride(effectiveCategory(qf))) && (
                                    <div
                                        className="text-[11px] mb-2"
                                        style={{ color: 'var(--fg-3)' }}
                                    >
                                        EPSG is optional and is only used when the file declares
                                        no coordinate system of its own — a shapefile shipped
                                        without its .prj, or a table of bare eastings and
                                        northings. A declared CRS always wins, and the geometry is
                                        checked against whatever code you give rather than trusted.
                                    </div>
                                )}
                                <ul className="text-xs space-y-1">
                                    {files.map((qf) => {
                                        const outcome = outcomes.find((o) => o.id === qf.id);
                                        const canOverrideCrs = supportsCrsOverride(
                                            effectiveCategory(qf),
                                        );
                                        const epsg = parseEpsg(qf.sourceEpsgText ?? '');
                                        const donated = qf.crsDonation;
                                        const usingDonation = donationInEffect(qf);
                                        // On a row using a donated coordinate
                                        // system the effective value is that
                                        // CRS, not an empty box asking for a
                                        // code. Typing one anyway wins: the
                                        // copy is dropped from the ZIP at
                                        // upload.
                                        const epsgPlaceholder =
                                            usingDonation && donated ? donated.label : '26904';
                                        const epsgTitle =
                                            usingDonation && donated
                                                ? `Using ${donated.label}, copied from ${donated.sourceName}. Type an EPSG code to use that instead — the copy is removed from this upload.`
                                                : 'Coordinate system to assume when the file declares none. EPSG number only, e.g. 26904. A CRS the file declares always wins.';
                                        return (
                                            <li key={qf.id} style={{ color: 'var(--fg-1)' }}>
                                                <div className="flex items-center gap-3">
                                                    <span className="font-mono">{qf.file.name}</span>
                                                    <span
                                                        className="font-mono"
                                                        style={{ color: 'var(--fg-3)' }}
                                                    >
                                                        {(qf.file.size / 1024).toFixed(1)} KB
                                                    </span>
                                                    {canOverrideCrs && (
                                                        <label className="flex items-center gap-1.5">
                                                            <span
                                                                className="text-[10px] font-mono uppercase tracking-wider"
                                                                style={{ color: 'var(--fg-3)' }}
                                                            >
                                                                EPSG
                                                            </span>
                                                            <input
                                                                type="text"
                                                                inputMode="numeric"
                                                                value={qf.sourceEpsgText ?? ''}
                                                                onChange={(e) =>
                                                                    setSourceEpsg(qf.id, e.target.value)
                                                                }
                                                                disabled={submitting || outcome?.ok}
                                                                placeholder={epsgPlaceholder}
                                                                title={epsgTitle}
                                                                aria-label={`Source EPSG for ${qf.file.name}`}
                                                                className="w-20 text-[11px] font-mono px-1.5 py-0.5 rounded border"
                                                                style={{
                                                                    background: 'var(--bg-2)',
                                                                    borderColor: epsg.error
                                                                        ? 'var(--danger, oklch(0.65 0.2 30))'
                                                                        : 'var(--line-2)',
                                                                    color: 'var(--fg-0)',
                                                                }}
                                                            />
                                                        </label>
                                                    )}
                                                    {outcome && (
                                                        <Pill
                                                            tone={outcome.ok ? 'accent' : 'warn'}
                                                            dot
                                                        >
                                                            {outcome.ok
                                                                ? 'queued'
                                                                : outcome.message}
                                                        </Pill>
                                                    )}
                                                    {!submitting && (!outcome || !outcome.ok) && (
                                                        <button
                                                            type="button"
                                                            onClick={() => removeFile(qf.id)}
                                                            aria-label={`Remove ${qf.file.name}`}
                                                            className="text-[10px] font-mono uppercase tracking-wider"
                                                            style={{ color: 'var(--fg-3)' }}
                                                        >
                                                            remove
                                                        </button>
                                                    )}
                                                </div>
                                                {/* The bundler's verdict is a note, not a
                                                    failure: the file still uploads. Rendered
                                                    in its own line rather than crammed into
                                                    the outcome pill, which is reserved for
                                                    what the server actually said.

                                                    A row that was given a coordinate system
                                                    reads as resolved and says where the CRS
                                                    came from — the bundler's own words while
                                                    the copy is in the ZIP, and this screen's
                                                    when the user has taken it back out. It
                                                    must never go back to nagging for an EPSG
                                                    code it is no longer missing. */}
                                                {donated ? (
                                                    <>
                                                        <div
                                                            className="text-[11px] mt-0.5"
                                                            style={{
                                                                color: usingDonation
                                                                    ? 'var(--fg-2)'
                                                                    : 'var(--warn)',
                                                            }}
                                                        >
                                                            {usingDonation
                                                                ? (qf.bundleNote ??
                                                                  `Coordinate system ${donated.label}, copied from ${donated.sourceName}.`)
                                                                : hasExplicitEpsg(qf)
                                                                  ? `EPSG ${epsg.epsg} replaces the coordinate system copied from ${donated.sourceName}: the copy is dropped from this upload, so the code you typed is the one that lands.`
                                                                  : donated.memberName
                                                                    ? `Not using the coordinate system from ${donated.sourceName}. This dataset has no .prj of its own — set an EPSG code on this row, or the ingest will refuse it.`
                                                                    : `Not using the coordinate system from ${donated.sourceName}. This format carries none of its own — set an EPSG code on this row, or its features are stored as 'assumed' with their position uncertain.`}
                                                        </div>
                                                        {/* The bundler's verdict is not only
                                                            about the CRS: most recipients in a
                                                            real delivery are missing their .dbf
                                                            as well. While the copy is in the ZIP
                                                            that verdict IS the line above, but
                                                            once the user declines it this
                                                            screen's sentence must be ADDED to
                                                            the verdict, not substituted for it —
                                                            otherwise the .dbf warning disappears
                                                            at exactly the moment the row needs
                                                            the most attention. */}
                                                        {!usingDonation && qf.bundleNote && (
                                                            <div
                                                                className="text-[11px] mt-0.5"
                                                                style={{ color: 'var(--warn)' }}
                                                            >
                                                                {qf.bundleNote}
                                                            </div>
                                                        )}
                                                    </>
                                                ) : (
                                                    qf.bundleNote && (
                                                        <div
                                                            className="text-[11px] mt-0.5"
                                                            style={{ color: 'var(--warn)' }}
                                                        >
                                                            {qf.bundleNote}
                                                        </div>
                                                    )
                                                )}
                                                {epsg.error && (
                                                    <div
                                                        className="text-[11px] mt-0.5"
                                                        style={{
                                                            color: 'var(--danger, oklch(0.65 0.2 30))',
                                                        }}
                                                    >
                                                        {epsg.error}
                                                    </div>
                                                )}
                                            </li>
                                        );
                                    })}
                                </ul>
                            </div>
                        )}
                    </Card>

                    {finished && failedCount > 0 && (
                        <div
                            className="flex items-center gap-3 text-xs px-3 py-2.5 rounded border"
                            style={{
                                color: 'var(--fg-1)',
                                borderColor: 'var(--warn)',
                                background: 'color-mix(in oklch, var(--warn) 8%, transparent)',
                            }}
                        >
                            <Pill tone="warn" dot>
                                {queuedCount} queued · {failedCount} failed
                            </Pill>
                            <span style={{ color: 'var(--fg-2)' }}>
                                Failed files stay listed above — fix and retry, or
                            </span>
                            {selectedProject && queuedCount > 0 && (
                                <Link
                                    href={`/projects/${selectedProject.slug}/ingestion-runs`}
                                    className="font-mono uppercase tracking-wider underline"
                                    style={{ color: 'var(--accent)' }}
                                >
                                    watch the {queuedCount} queued file{queuedCount === 1 ? '' : 's'} →
                                </Link>
                            )}
                        </div>
                    )}

                    <footer className="flex justify-end items-center gap-3">
                        {epsgErrorCount > 0 && (
                            <span
                                className="text-xs"
                                style={{ color: 'var(--danger, oklch(0.65 0.2 30))' }}
                            >
                                Fix {epsgErrorCount} EPSG code{epsgErrorCount === 1 ? '' : 's'}{' '}
                                before uploading.
                            </span>
                        )}
                        <button
                            type="button"
                            disabled={
                                !selectedProject ||
                                files.length === 0 ||
                                submitting ||
                                epsgErrorCount > 0
                            }
                            onClick={handleSubmit}
                            className="text-[10px] font-mono uppercase tracking-wider px-3 py-1.5 rounded border disabled:opacity-30"
                            style={{
                                color: 'var(--bg-0)',
                                background: 'var(--accent)',
                                borderColor: 'var(--accent-dim)',
                            }}
                        >
                            {submitting ? 'Uploading…' : 'Start ingest →'}
                        </button>
                    </footer>
                </div>
            </div>
        </AppLayout>
    );
}
