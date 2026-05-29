import type { JSX } from 'react';
import { useEffect, useRef, useState } from 'react';
import { Head, router } from '@inertiajs/react';
import maplibregl, { type Map as MlMap } from 'maplibre-gl';
import AppLayout from '../../Layouts/AppLayout';

/**
 * §8.5 Customer Onboarding Wizard.
 *
 * 4-step funnel:
 *   1. Workspace identity (name + commodity + region)
 *   2. Project (name + optional AOI polygon on map)
 *   3. Data — upload OR skip
 *   4. First chat seed — show pre-filled prompts; "Open your chat"
 *
 * The full master-plan §8.5 spec calls for 7 steps including OAuth-based
 * cloud-source watchers + real-time ingestion progress + first-report
 * prompt. This 4-step version covers the "get to first answer" path;
 * OAuth + ingestion progress + first-report-prompt arrive in v2.
 */

interface PageProps {
    commodities: string[];
    regions: Record<string, string>;
    progress: {
        step1?: { workspace_name: string; commodity: string; region: string };
        step2?: { project_name: string; project_id: string; slug: string };
        completed_steps?: string[];
    };
    user_email: string | null;
}

function csrfToken(): string {
    return (document.querySelector('meta[name="csrf-token"]') as HTMLMetaElement)?.content ?? '';
}

async function post(path: string, body: unknown): Promise<Response> {
    return fetch(path, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRF-TOKEN': csrfToken(),
            Accept: 'application/json',
        },
        body: JSON.stringify(body),
    });
}

const SUGGESTED_PROMPTS: Record<string, string[]> = {
    uranium: [
        'What is the average grade of uranium in my project area?',
        'Show me drill holes near my AOI.',
        'Generate a Workspace Health snapshot.',
    ],
    gold: [
        'What gold occurrences are within 25km of my project?',
        'Show me the most recent assay results.',
        'Are there any historic producers in my AOI?',
    ],
    copper: [
        'What copper deposits are nearby?',
        'Summarize bedrock geology in my AOI.',
        'Show drillhole status across my project.',
    ],
};

function defaultPromptsFor(commodity: string): string[] {
    return SUGGESTED_PROMPTS[commodity] ?? [
        'What public-geoscience data is available in my project area?',
        'Show me drillholes within 50km of my AOI.',
        'Generate a Workspace Health snapshot.',
    ];
}

export default function OnboardingWizard({ commodities, regions, progress }: PageProps): JSX.Element {
    const startStep = (() => {
        const done = progress.completed_steps ?? [];
        if (done.includes('step4')) return 5;
        if (done.includes('step3')) return 4;
        if (done.includes('step2')) return 3;
        if (done.includes('step1')) return 2;
        return 1;
    })();
    const [step, setStep] = useState<number>(startStep);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // Step 1 state
    const [workspaceName, setWorkspaceName] = useState(progress.step1?.workspace_name ?? '');
    const [commodity, setCommodity] = useState(progress.step1?.commodity ?? 'uranium');
    const [region, setRegion] = useState(progress.step1?.region ?? 'CA-SK');

    // Step 2 state
    const [projectName, setProjectName] = useState(progress.step2?.project_name ?? '');
    const [aoiCoords, setAoiCoords] = useState<[number, number][]>([]);
    const [drawMode, setDrawMode] = useState<'none' | 'polygon'>('none');
    const mapContainer = useRef<HTMLDivElement | null>(null);
    const mapRef = useRef<MlMap | null>(null);

    // Step 3 state
    const [skipUpload, setSkipUpload] = useState(false);
    const [uploadedFiles, setUploadedFiles] = useState<FileList | null>(null);

    // Step 4 / completion
    const [projectId, setProjectId] = useState<string | null>(progress.step2?.project_id ?? null);

    // ─── Map init for step 2 ──────────────────────────────────────────
    useEffect(() => {
        if (step !== 2 || !mapContainer.current || mapRef.current) return;
        const centers: Record<string, [number, number]> = {
            'CA-SK': [-106.5, 55.0],
            'CA-BC': [-127.0, 54.0],
            'CA-NT': [-115.0, 64.0],
            'CA-YT': [-135.0, 64.0],
            'CA-ON': [-85.0, 50.0],
            'CA-QC': [-72.0, 52.0],
            'US':    [-98.0, 39.0],
            'OTHER': [0, 0],
        };
        const center = centers[region] ?? [-106.0, 55.0];

        const map = new maplibregl.Map({
            container: mapContainer.current,
            style: {
                version: 8,
                sources: {
                    osm: {
                        type: 'raster',
                        tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
                        tileSize: 256,
                        attribution: '© OpenStreetMap',
                    },
                },
                layers: [{ id: 'osm', type: 'raster', source: 'osm' }],
            },
            center,
            zoom: 5,
        });
        mapRef.current = map;

        map.on('load', () => {
            map.addSource('aoi', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
            map.addLayer({
                id: 'aoi-fill', type: 'fill', source: 'aoi',
                paint: { 'fill-color': '#a78bfa', 'fill-opacity': 0.3 },
            });
            map.addLayer({
                id: 'aoi-line', type: 'line', source: 'aoi',
                paint: { 'line-color': '#7c3aed', 'line-width': 2 },
            });
            map.addSource('aoi-pts', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
            map.addLayer({
                id: 'aoi-pts-circle', type: 'circle', source: 'aoi-pts',
                paint: { 'circle-radius': 5, 'circle-color': '#ef4444' },
            });
        });

        map.on('click', (e) => {
            if (drawMode !== 'polygon') return;
            setAoiCoords((prev) => [...prev, [e.lngLat.lng, e.lngLat.lat]]);
        });

        return () => {
            map.remove();
            mapRef.current = null;
        };
    }, [step, region]);

    // Update AOI layers when coords change
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !map.isStyleLoaded()) return;
        const ptsSrc = map.getSource('aoi-pts') as maplibregl.GeoJSONSource | undefined;
        if (ptsSrc) {
            ptsSrc.setData({
                type: 'FeatureCollection',
                features: aoiCoords.map((c) => ({
                    type: 'Feature', geometry: { type: 'Point', coordinates: c }, properties: {},
                })),
            });
        }
        const polySrc = map.getSource('aoi') as maplibregl.GeoJSONSource | undefined;
        if (polySrc && aoiCoords.length >= 3) {
            const closed = [...aoiCoords, aoiCoords[0]];
            polySrc.setData({
                type: 'FeatureCollection',
                features: [{
                    type: 'Feature',
                    geometry: { type: 'Polygon', coordinates: [closed] },
                    properties: {},
                }],
            });
        } else if (polySrc) {
            polySrc.setData({ type: 'FeatureCollection', features: [] });
        }
    }, [aoiCoords]);

    // ─── Submit handlers ─────────────────────────────────────────────
    async function submitStep1() {
        if (!workspaceName.trim()) { setError('Workspace name required'); return; }
        setBusy(true); setError(null);
        try {
            const r = await post('/onboarding/step1', {
                workspace_name: workspaceName,
                commodity,
                region,
            });
            if (!r.ok) throw new Error(await r.text());
            setStep(2);
        } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
        finally { setBusy(false); }
    }

    async function submitStep2() {
        if (!projectName.trim()) { setError('Project name required'); return; }
        setBusy(true); setError(null);
        try {
            const aoi = aoiCoords.length >= 3
                ? { type: 'Polygon', coordinates: [[...aoiCoords, aoiCoords[0]]] }
                : null;
            const r = await post('/onboarding/step2', {
                project_name: projectName,
                aoi_geojson: aoi,
            });
            if (!r.ok) throw new Error(await r.text());
            const j = await r.json();
            setProjectId(j.project_id);
            setStep(3);
        } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
        finally { setBusy(false); }
    }

    // Map file extension → upload category (matches CATEGORIES in UploadController).
    function categoryForFile(filename: string): string | null {
        const ext = filename.toLowerCase().split('.').pop() ?? '';
        if (ext === 'pdf')                       return 'reports';
        if (['csv'].includes(ext))               return 'collars'; // user can change in cluster-ingest later
        if (ext === 'las')                       return 'well_logs';
        if (['xlsx', 'xls'].includes(ext))       return 'excel';
        if (['sgy', 'segy'].includes(ext))       return 'seismic';
        if (['geojson', 'shp', 'zip'].includes(ext)) return 'spatial';
        return null;
    }

    async function uploadOne(file: File): Promise<{ ok: boolean; reason?: string }> {
        if (!projectId) return { ok: false, reason: 'no project_id' };
        const cat = categoryForFile(file.name);
        if (!cat) return { ok: false, reason: `unsupported file type: ${file.name}` };
        const fd = new FormData();
        fd.append('file', file);
        fd.append('category', cat);
        try {
            const r = await fetch(`/api/v1/projects/${projectId}/upload`, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'X-CSRF-TOKEN': csrfToken(),
                    Accept: 'application/json',
                },
                body: fd,
            });
            if (!r.ok) {
                const txt = await r.text();
                return { ok: false, reason: `${r.status}: ${txt.slice(0, 200)}` };
            }
            return { ok: true };
        } catch (e) {
            return { ok: false, reason: e instanceof Error ? e.message : String(e) };
        }
    }

    async function submitStep3() {
        setBusy(true); setError(null);
        const files = uploadedFiles;
        const isSkipping = skipUpload || !files || files.length === 0;

        try {
            let uploadedCount = 0;
            const failures: string[] = [];
            if (!isSkipping && files) {
                // Fire uploads sequentially so the operator sees clear progress
                // (parallel would race the workspace_id resolution + RLS GUC).
                for (let i = 0; i < files.length; i++) {
                    const result = await uploadOne(files[i]);
                    if (result.ok) uploadedCount++;
                    else failures.push(`${files[i].name} → ${result.reason}`);
                }
                if (failures.length > 0) {
                    setError(`Uploaded ${uploadedCount} of ${files.length}; failures: ${failures.join('; ')}`);
                    // Continue anyway — partial upload still moves the user forward
                }
            }

            const r = await post('/onboarding/step3', {
                skipped: isSkipping,
                file_count: uploadedCount,
            });
            if (!r.ok) throw new Error(await r.text());
            setStep(4);
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    }

    function complete() {
        // POST /onboarding/complete is a normal form post that redirects
        router.post('/onboarding/complete', {});
    }

    const stepLabel = (n: number, label: string): JSX.Element => (
        <div className="flex items-center gap-2">
            <span className={`inline-flex h-6 w-6 items-center justify-center rounded-full text-xs font-medium ${
                n === step ? 'bg-indigo-600 text-white' :
                n < step  ? 'bg-emerald-500 text-white' :
                            'bg-zinc-200 text-zinc-500'
            }`}>
                {n < step ? '✓' : n}
            </span>
            <span className={n === step ? 'font-medium text-zinc-900' : 'text-zinc-500'}>{label}</span>
        </div>
    );

    return (
        <AppLayout>
            <Head title="Welcome to GeoRAG" />

            <div className="mx-auto max-w-4xl px-4 py-8">
                <div className="rounded-lg border border-zinc-200 bg-white shadow-sm">
                    <div className="border-b border-zinc-200 px-6 py-4">
                        <h1 className="text-xl font-semibold text-zinc-900">Welcome to GeoRAG</h1>
                        <p className="mt-1 text-sm text-zinc-500">
                            Let's get your workspace set up — about 5 minutes.
                        </p>
                        <div className="mt-4 flex items-center gap-6">
                            {stepLabel(1, 'Workspace')}
                            <div className="h-px w-8 bg-zinc-200" />
                            {stepLabel(2, 'Project + AOI')}
                            <div className="h-px w-8 bg-zinc-200" />
                            {stepLabel(3, 'Data')}
                            <div className="h-px w-8 bg-zinc-200" />
                            {stepLabel(4, 'First chat')}
                            <div className="h-px w-8 bg-zinc-200" />
                            {stepLabel(5, 'First report')}
                        </div>
                    </div>

                    <div className="px-6 py-5">
                        {error && (
                            <div className="mb-3 rounded border border-red-200 bg-red-50 p-2 text-sm text-red-800">
                                {error}
                            </div>
                        )}

                        {step === 1 && (
                            <div>
                                <h2 className="text-base font-medium">Tell us about your workspace</h2>
                                <p className="text-sm text-zinc-500">
                                    Your commodity choice pre-loads the matching deposit-model template.
                                </p>
                                <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
                                    <label className="block sm:col-span-2">
                                        <span className="text-xs font-medium text-zinc-700">Workspace name</span>
                                        <input
                                            type="text"
                                            value={workspaceName}
                                            onChange={(e) => setWorkspaceName(e.target.value)}
                                            placeholder="e.g. Athabasca Exploration"
                                            className="mt-1 block w-full rounded-md border-zinc-300 text-sm"
                                            autoFocus
                                        />
                                    </label>
                                    <label className="block">
                                        <span className="text-xs font-medium text-zinc-700">Primary commodity</span>
                                        <select
                                            value={commodity}
                                            onChange={(e) => setCommodity(e.target.value)}
                                            className="mt-1 block w-full rounded-md border-zinc-300 text-sm"
                                        >
                                            {commodities.map((c) => <option key={c} value={c}>{c}</option>)}
                                        </select>
                                    </label>
                                    <label className="block">
                                        <span className="text-xs font-medium text-zinc-700">Region</span>
                                        <select
                                            value={region}
                                            onChange={(e) => setRegion(e.target.value)}
                                            className="mt-1 block w-full rounded-md border-zinc-300 text-sm"
                                        >
                                            {Object.entries(regions).map(([k, v]) =>
                                                <option key={k} value={k}>{v}</option>
                                            )}
                                        </select>
                                    </label>
                                </div>
                                <div className="mt-6 flex justify-end">
                                    <button
                                        type="button"
                                        onClick={submitStep1}
                                        disabled={busy}
                                        className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
                                    >
                                        {busy ? 'Saving…' : 'Next →'}
                                    </button>
                                </div>
                            </div>
                        )}

                        {step === 2 && (
                            <div>
                                <h2 className="text-base font-medium">Create your first project</h2>
                                <p className="text-sm text-zinc-500">
                                    Optionally draw your area of interest on the map. Click to add vertices.
                                </p>
                                <div className="mt-4">
                                    <label className="block">
                                        <span className="text-xs font-medium text-zinc-700">Project name</span>
                                        <input
                                            type="text"
                                            value={projectName}
                                            onChange={(e) => setProjectName(e.target.value)}
                                            placeholder="e.g. Wollaston Lake Discovery"
                                            className="mt-1 block w-full rounded-md border-zinc-300 text-sm"
                                            autoFocus
                                        />
                                    </label>
                                </div>
                                <div className="mt-4 flex items-center gap-2">
                                    <button
                                        type="button"
                                        onClick={() => setDrawMode(drawMode === 'polygon' ? 'none' : 'polygon')}
                                        className={`rounded px-3 py-1 text-xs ${drawMode === 'polygon' ? 'bg-violet-600 text-white' : 'bg-violet-100 text-violet-800'}`}
                                    >
                                        {drawMode === 'polygon' ? 'Stop drawing' : 'Draw AOI'}
                                    </button>
                                    {aoiCoords.length > 0 && (
                                        <>
                                            <span className="text-xs text-zinc-500">{aoiCoords.length} vertices</span>
                                            <button
                                                type="button"
                                                onClick={() => setAoiCoords([])}
                                                className="text-xs text-zinc-500 underline"
                                            >
                                                Clear
                                            </button>
                                        </>
                                    )}
                                </div>
                                <div ref={mapContainer} className="mt-3 h-72 w-full rounded border border-zinc-200" />
                                <div className="mt-6 flex justify-between">
                                    <button
                                        type="button"
                                        onClick={() => setStep(1)}
                                        className="rounded border border-zinc-300 px-4 py-2 text-sm"
                                    >
                                        ← Back
                                    </button>
                                    <button
                                        type="button"
                                        onClick={submitStep2}
                                        disabled={busy}
                                        className="rounded bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
                                    >
                                        {busy ? 'Creating…' : 'Next →'}
                                    </button>
                                </div>
                            </div>
                        )}

                        {step === 3 && (
                            <div>
                                <h2 className="text-base font-medium">Bring in some data</h2>
                                <p className="text-sm text-zinc-500">
                                    Upload PDFs, CSVs, or shapefiles — or skip to try GeoRAG against your project's
                                    public-geoscience overlay first. You can always add data later from the
                                    project page.
                                </p>
                                <div className="mt-4 rounded border border-dashed border-zinc-300 p-6 text-center">
                                    <input
                                        type="file"
                                        multiple
                                        accept=".pdf,.csv,.las,.xlsx,.xls,.sgy,.segy,.geojson,.shp,.zip"
                                        onChange={(e) => setUploadedFiles(e.target.files)}
                                        className="text-sm"
                                    />
                                    {uploadedFiles && (
                                        <p className="mt-2 text-xs text-zinc-500">
                                            {uploadedFiles.length} file(s) selected — will upload + trigger ingest when you click "Next"
                                        </p>
                                    )}
                                </div>
                                <p className="mt-2 text-[11px] text-zinc-400">
                                    Supported: PDF (reports), CSV (collars), LAS (well logs), XLSX, SEG-Y, GeoJSON/SHP/ZIP (spatial)
                                </p>
                                <label className="mt-3 inline-flex items-center gap-2 text-sm">
                                    <input
                                        type="checkbox"
                                        checked={skipUpload}
                                        onChange={(e) => setSkipUpload(e.target.checked)}
                                    />
                                    Skip upload — I just want to try the chat first
                                </label>
                                <div className="mt-6 flex justify-between">
                                    <button
                                        type="button"
                                        onClick={() => setStep(2)}
                                        className="rounded border border-zinc-300 px-4 py-2 text-sm"
                                    >
                                        ← Back
                                    </button>
                                    <button
                                        type="button"
                                        onClick={submitStep3}
                                        disabled={busy}
                                        className="rounded bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
                                    >
                                        {busy ? 'Saving…' : 'Next →'}
                                    </button>
                                </div>
                            </div>
                        )}

                        {step === 4 && (
                            <div>
                                <h2 className="text-base font-medium">Ready to ask your first question</h2>
                                <p className="text-sm text-zinc-500">
                                    Every answer GeoRAG gives carries citations to the underlying data. Try one of
                                    these prompts — they're tailored to your {commodity} workspace:
                                </p>
                                <ul className="mt-4 space-y-2">
                                    {defaultPromptsFor(commodity).map((p, i) => (
                                        <li key={i} className="rounded border border-indigo-200 bg-indigo-50 px-3 py-2 text-sm text-indigo-900">
                                            "{p}"
                                        </li>
                                    ))}
                                </ul>
                                <div className="mt-6 flex justify-between">
                                    <button
                                        type="button"
                                        onClick={() => setStep(3)}
                                        className="rounded border border-zinc-300 px-4 py-2 text-sm"
                                    >
                                        ← Back
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => setStep(5)}
                                        className="rounded bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500"
                                    >
                                        Next →
                                    </button>
                                </div>
                            </div>
                        )}

                        {step === 5 && (
                            <div>
                                <h2 className="text-base font-medium">Optional — generate your first report</h2>
                                <p className="text-sm text-zinc-500">
                                    The Workspace Health Snapshot is a free, no-sign-off report that summarises
                                    what's in your workspace, what public data overlaps your AOI, and where the
                                    data gaps are. It takes about 30 seconds to generate.
                                </p>
                                <div className="mt-4 rounded-lg border border-indigo-200 bg-indigo-50 p-4">
                                    <div className="flex items-center justify-between">
                                        <div>
                                            <div className="text-sm font-semibold text-indigo-900">
                                                Workspace Health Snapshot
                                            </div>
                                            <div className="text-xs text-indigo-700">
                                                Inventory · Public overlay · Data gaps · No QP sign-off required
                                            </div>
                                        </div>
                                        <a
                                            href={projectId ? `/admin/reports/build?template=workspace_health&project_id=${projectId}` : '/admin/reports/build?template=workspace_health'}
                                            target="_blank" rel="noopener noreferrer"
                                            className="rounded bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-500"
                                        >
                                            Generate →
                                        </a>
                                    </div>
                                </div>
                                <p className="mt-3 text-xs text-zinc-500">
                                    You can also skip this and generate reports later from the Reports cockpit.
                                </p>
                                <div className="mt-6 flex justify-between">
                                    <button
                                        type="button"
                                        onClick={() => setStep(4)}
                                        className="rounded border border-zinc-300 px-4 py-2 text-sm"
                                    >
                                        ← Back
                                    </button>
                                    <button
                                        type="button"
                                        onClick={complete}
                                        className="rounded bg-emerald-600 px-5 py-2 text-sm font-medium text-white hover:bg-emerald-500"
                                    >
                                        Finish · Open my chat →
                                    </button>
                                </div>
                            </div>
                        )}
                    </div>
                </div>

                {projectId && step >= 3 && (
                    <p className="mt-3 text-center text-xs text-zinc-500">
                        Project created · ID <code>{projectId}</code>
                    </p>
                )}
            </div>
        </AppLayout>
    );
}
