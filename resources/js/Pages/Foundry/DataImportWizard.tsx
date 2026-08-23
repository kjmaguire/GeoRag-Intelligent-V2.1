import { useEffect, useRef, useState } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill } from '@/Components/Foundry/primitives';
import {
    acceptedExtensions,
    categoryForExtension,
    parseEpsg,
    supportsCrsOverride,
    type Category,
} from '@/lib/uploadCategories';
import { BUNDLE_MEMBER_EXTS, groupShapefiles } from '@/lib/shapefileBundle';

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

/** A queued file with a stable per-entry id so duplicate filenames never
 *  collide when matching upload outcomes back to rows. */
interface QueuedFile {
    id: string;
    file: File;
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

export default function FoundryDataImportWizard() {
    const [projects, setProjects] = useState<ProjectPick[] | null>(null);
    const [projectsError, setProjectsError] = useState<string | null>(null);
    const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null);
    const [files, setFiles] = useState<QueuedFile[]>([]);
    const [rejectedNote, setRejectedNote] = useState<string | null>(null);
    /** Incomplete-set verdicts from the bundler, and orphaned members kept
     *  with the reason they could not be attached to anything. */
    const [bundleNotes, setBundleNotes] = useState<string[]>([]);
    const [dragging, setDragging] = useState(false);
    const [submitting, setSubmitting] = useState(false);
    const [outcomes, setOutcomes] = useState<UploadOutcome[]>([]);
    const [finished, setFinished] = useState(false);
    const fileInputRef = useRef<HTMLInputElement | null>(null);

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
        const arr = Array.from(incoming);
        if (arr.length === 0) return;
        const accepted: QueuedFile[] = [];
        const rejected: string[] = [];

        // Zip each .shp back together with its .shx/.dbf/.prj siblings before
        // anything else looks at the list. Uploaded alone, a .shp cannot be
        // parsed; the sidecars have no category of their own and would
        // otherwise be rejected here as unsupported.
        // Falling back to the raw list keeps a zip failure (out of memory on a
        // very large .dbf, say) from swallowing the whole selection: the .shp
        // is queued and fails server-side with a message, which beats a drop
        // zone that silently does nothing.
        const { bundles, passthrough, unusable } = await groupShapefiles(arr).catch(
            () => ({ bundles: [], passthrough: arr, unusable: [] }),
        );
        const notes: string[] = [];
        for (const b of bundles) {
            accepted.push({
                id: newQueueId(),
                file: b.file,
                category: 'spatial',
                bundleNote: b.verdict ?? undefined,
            });
            if (b.verdict) notes.push(`${b.stem}: ${b.verdict}`);
        }
        // An orphaned sidecar is NOT lumped in with "unsupported file type".
        // It is a supported format whose master was not selected, and saying
        // which master is missing is the difference between a user fixing the
        // selection and a user concluding the drop zone is broken.
        for (const u of unusable) notes.push(`${u.file.name}: ${u.reason}`);

        for (const f of passthrough) {
            if (ACCEPTED_EXTENSIONS.includes(fileExtension(f.name))) {
                accepted.push({ id: newQueueId(), file: f });
            } else {
                rejected.push(f.name);
            }
        }
        if (accepted.length > 0) {
            setFiles((prev) => [...prev, ...accepted]);
        }
        setRejectedNote(
            rejected.length > 0
                ? `Skipped ${rejected.length} unsupported file${rejected.length === 1 ? '' : 's'} (${rejected
                      .slice(0, 3)
                      .join(', ')}${rejected.length > 3 ? ', …' : ''}) — accepted types: ${ACCEPTED_EXTENSIONS.join(', ')}.`
                : null,
        );
        setBundleNotes(notes);
        // Keep successful outcomes (their pills + the no-re-upload guard in
        // handleSubmit depend on them); clear stale failures so the new
        // attempt starts clean.
        setOutcomes((prev) => prev.filter((o) => o.ok));
        setFinished(false);
    }

    function removeFile(id: string) {
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

    function onDrop(e: React.DragEvent<HTMLDivElement>) {
        e.preventDefault();
        setDragging(false);
        if (submitting) return;
        addFiles(e.dataTransfer?.files ?? null);
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
        const fd = new FormData();
        fd.append('file', qf.file);
        fd.append('category', category);
        // `source_epsg`, an integer, and only when the user typed a legal one
        // for a category whose trigger carries it. Same name and same type as
        // the field the tabular ingest has always taken, so the two paths
        // that share this screen cannot end up with two names for one idea.
        const epsg = parseEpsg(qf.sourceEpsgText ?? '');
        if (supportsCrsOverride(category) && epsg.epsg !== undefined) {
            fd.append('source_epsg', String(epsg.epsg));
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

                        {rejectedNote && (
                            <div
                                className="mt-3 text-xs px-3 py-2 rounded border"
                                style={{
                                    color: 'var(--warn)',
                                    borderColor: 'var(--warn)',
                                    background: 'color-mix(in oklch, var(--warn) 10%, transparent)',
                                }}
                            >
                                {rejectedNote}
                            </div>
                        )}

                        {/* Incomplete-set verdicts. Deliberately NOT folded
                            into the "unsupported file" line above: these are
                            supported formats that arrived without the members
                            GDAL needs, and the fix is to re-drop the folder,
                            not to give up on the format. Nothing here was
                            discarded — the bundles still upload. */}
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
                                    Incomplete file sets · {bundleNotes.length}
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
                                                                placeholder="26904"
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
                                                    what the server actually said. */}
                                                {qf.bundleNote && (
                                                    <div
                                                        className="text-[11px] mt-0.5"
                                                        style={{ color: 'var(--warn)' }}
                                                    >
                                                        {qf.bundleNote}
                                                    </div>
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
