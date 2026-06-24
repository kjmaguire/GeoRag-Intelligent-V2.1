import { useCallback, useMemo, useRef, useState } from 'react';
import { Head } from '@inertiajs/react';
import JSZip from 'jszip';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card } from '@/Components/Foundry/primitives';

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

// Backend categories accepted by POST /api/v1/projects/{id}/upload.
// Source of truth: App\Http\Controllers\Api\V1\UploadController::CATEGORIES.
type Category =
    | 'collars' | 'surveys' | 'lithology' | 'samples'
    | 'reports' | 'well_logs' | 'spatial' | 'excel'
    | 'seismic' | 'xyz';

const CATEGORY_LABEL: Record<Category, string> = {
    collars:   'Drill collars (CSV)',
    surveys:   'Down-hole surveys (CSV)',
    lithology: 'Lithology logs (CSV)',
    samples:   'Assay samples (CSV)',
    reports:   'NI 43-101 / reports (PDF)',
    well_logs: 'Well logs (LAS)',
    spatial:   'Spatial / GIS (GeoJSON, SHP, KMZ, ZIP)',
    excel:     'Excel workbooks (XLSX)',
    seismic:   'Seismic (SEG-Y)',
    xyz:       'XYZ grids / point data',
};

const CATEGORY_EXTS: Record<Category, string[]> = {
    collars: ['csv'],
    surveys: ['csv'],
    lithology: ['csv'],
    samples: ['csv'],
    reports: ['pdf', 'tif', 'tiff'],
    well_logs: ['las'],
    spatial: ['geojson', 'shp', 'zip', 'kmz', 'kml'], // kmz/kml accepted at backend? falls under spatial; backend allows geojson/shp/zip.
    excel: ['xlsx', 'xls'],
    seismic: ['sgy', 'segy'],
    xyz: ['xyz', 'dat', 'txt'],
};

// Extensions the backend's UploadController will outright reject. TIFF scans
// route through `reports` → tiff_normalize per ADR-0005 and are supported.
const UNSUPPORTED_EXTS = new Set(['jpg', 'jpeg', 'png', 'gif', 'bmp']);

const MAX_FILE_BYTES = 6 * 1024 * 1024 * 1024; // 6 GB — matches UploadController + Octane limits (ZIP archive support)

function extOf(name: string): string {
    return name.split('.').pop()?.toLowerCase() ?? '';
}

function autoCategory(ext: string): Category | null {
    if (UNSUPPORTED_EXTS.has(ext)) return null;
    for (const [cat, exts] of Object.entries(CATEGORY_EXTS) as [Category, string[]][]) {
        if (exts.includes(ext)) return cat;
    }
    return null;
}

function humanSize(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

interface QueuedFile {
    id: string;
    file: File;
    name: string;
    size: number;
    ext: string;
    category: Category | null; // null = unsupported
    status: 'queued' | 'uploading' | 'done' | 'error';
    error?: string;
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

    const addFiles = useCallback(async (files: FileList | File[]) => {
        const arr: QueuedFile[] = [];
        const skippedNames: string[] = [];
        for (const f of Array.from(files)) {
            const ext = extOf(f.name);
            // Drop files we can't categorise (unknown extension, raster images,
            // or 0-byte folder shells dragged in instead of using Select Folder).
            // Keep ZIPs — they're handled below with their own error message.
            if (ext !== 'zip' && (autoCategory(ext) === null || f.size === 0)) {
                skippedNames.push(f.name);
                continue;
            }
            // Large ZIPs can't be extracted in-browser (memory limit).
            // Queue as a normal file but flag it so the UI shows a tip to use Select Folder.
            if (ext === 'zip') {
                arr.push({
                    id: newId(),
                    file: f,
                    name: f.name,
                    size: f.size,
                    ext,
                    category: 'spatial', // ZIP is valid; spatial is the right bucket
                    status: 'error',
                    error: 'Extract this ZIP on your computer first, then use "📁 Select Folder" to load all files at once.',
                });
                continue;
            }
            arr.push({
                id: newId(),
                file: f,
                name: f.name,
                size: f.size,
                ext,
                category: autoCategory(ext),
                status: 'queued',
            });
        }
        setQueue((q) => [...q, ...arr]);
        if (skippedNames.length > 0) {
            setSkipped({ names: skippedNames });
        }
    }, []);

    const removeFile = (id: string) => setQueue((q) => q.filter((x) => x.id !== id));
    const setCategory = (id: string, cat: Category) =>
        setQueue((q) => q.map((x) => (x.id === id ? { ...x, category: cat } : x)));

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
        return { ok, unsupported, oversize, bytes };
    }, [queue]);

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
            const uploadable = queue.filter(
                (q) => q.category !== null && q.size <= MAX_FILE_BYTES,
            );
            setSubmitProgress({ done: 0, total: uploadable.length });

            let done = 0;
            for (const qf of uploadable) {
                setQueue((q) => q.map((x) => (x.id === qf.id ? { ...x, status: 'uploading' } : x)));
                const fd = new FormData();
                fd.append('file', qf.file);
                fd.append('category', qf.category as string);
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

            // 3. Land the user on the project overview.
            // Route is /projects/{slug} (see routes/web.php — OverviewController),
            // NOT /foundry/projects/{id} (that prefix only exists for /new).
            window.location.href = `/projects/${projectSlug ?? projectId}`;
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
                                    Per-file cap: 100&nbsp;MB.
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
                                        CSV · PDF · LAS · GeoJSON / SHP · KMZ · XLSX · SEG-Y · XYZ
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
                                    title="Cloud URL fetch isn't wired yet. Use the OAuth connectors at /oauth/connections for SharePoint / OneDrive / Google Drive sync."
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
                                                    <span style={{ color: 'var(--danger, oklch(0.65 0.2 30))' }}> · {queueSummary.oversize.length} over 100 MB</span>
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
                                                return (
                                                    <li key={q.id} className="grid grid-cols-[1fr_140px_70px_auto] items-center gap-2 px-3 py-1.5" style={{ background: 'var(--bg-1)' }}>
                                                        <div className="min-w-0">
                                                            <div className="text-xs truncate" style={{ color: 'var(--fg-0)' }}>{q.name}</div>
                                                            <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                                                                .{q.ext} · {humanSize(q.size)}
                                                                {q.parentZip && <> · <span title={`Extracted from ${q.parentZip}`} style={{ color: 'var(--fg-2)' }}>from {q.parentZip}</span></>}
                                                                {q.status !== 'queued' && <> · <span style={{ color: q.status === 'done' ? 'var(--accent)' : q.status === 'error' ? 'var(--danger, oklch(0.65 0.2 30))' : 'var(--fg-2)' }}>{q.status}</span></>}
                                                                {q.error && <> · <span title={q.error} style={{ color: 'var(--danger, oklch(0.65 0.2 30))' }}>{q.error.slice(0, 40)}</span></>}
                                                            </div>
                                                        </div>
                                                        {unsupported ? (
                                                            <span className="text-[10px] font-mono uppercase tracking-wider px-2 py-0.5 rounded border text-center" style={{ color: 'var(--warn, oklch(0.78 0.18 75))', borderColor: 'var(--warn, oklch(0.78 0.18 75))' }}>
                                                                raster · not supported
                                                            </span>
                                                        ) : (
                                                            <select
                                                                value={q.category as string}
                                                                onChange={(e) => setCategory(q.id, e.target.value as Category)}
                                                                disabled={q.status === 'uploading' || q.status === 'done'}
                                                                className="text-[11px] px-2 py-1 rounded border"
                                                                style={inputStyle}
                                                            >
                                                                {(Object.keys(CATEGORY_LABEL) as Category[])
                                                                    .filter((cat) => CATEGORY_EXTS[cat].includes(q.ext))
                                                                    .map((cat) => (
                                                                        <option key={cat} value={cat}>{CATEGORY_LABEL[cat]}</option>
                                                                    ))}
                                                                {/* If no category exactly matches the ext, still allow forcing one */}
                                                                {(Object.keys(CATEGORY_LABEL) as Category[]).every((cat) => !CATEGORY_EXTS[cat].includes(q.ext)) &&
                                                                    (Object.keys(CATEGORY_LABEL) as Category[]).map((cat) => (
                                                                        <option key={cat} value={cat}>{CATEGORY_LABEL[cat]}</option>
                                                                    ))
                                                                }
                                                            </select>
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
                                                {' · '}{queueSummary.oversize.length} over 100 MB will be skipped
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
                                disabled={submitting || !form.name}
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
