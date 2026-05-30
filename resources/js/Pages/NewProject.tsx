// @ts-nocheck
import { useState, useCallback } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '../Layouts/AppLayout';

/**
 * NewProject — multi-step project creation wizard.
 *
 * Step 1: Project metadata (name, CRS, company, commodity, region)
 * Step 2: File upload (drag-and-drop to MinIO bronze bucket)
 *
 * On completion, redirects to the chat page with the new project selected.
 */

interface CrsOption {
    value: string;
    label: string;
}

const CRS_OPTIONS: CrsOption[] = [
    { value: 'EPSG:32607', label: 'UTM Zone 7N (BC coast)' },
    { value: 'EPSG:32608', label: 'UTM Zone 8N (Yukon south)' },
    { value: 'EPSG:32609', label: 'UTM Zone 9N (BC interior)' },
    { value: 'EPSG:32610', label: 'UTM Zone 10N (BC/Alberta)' },
    { value: 'EPSG:32611', label: 'UTM Zone 11N (Alberta/BC)' },
    { value: 'EPSG:32612', label: 'UTM Zone 12N (Alberta/Sask)' },
    { value: 'EPSG:32613', label: 'UTM Zone 13N (Wyoming / Sask west)' },
    { value: 'EPSG:32614', label: 'UTM Zone 14N (Manitoba)' },
    { value: 'EPSG:32615', label: 'UTM Zone 15N (Ontario west)' },
    { value: 'EPSG:32616', label: 'UTM Zone 16N (Ontario)' },
    { value: 'EPSG:32617', label: 'UTM Zone 17N (Ontario east)' },
    { value: 'EPSG:32618', label: 'UTM Zone 18N (Quebec)' },
    { value: 'EPSG:32619', label: 'UTM Zone 19N (Quebec east)' },
    { value: 'EPSG:32620', label: 'UTM Zone 20N (NB/NS)' },
    { value: 'EPSG:32621', label: 'UTM Zone 21N (Newfoundland)' },
    { value: 'EPSG:4326',  label: 'WGS 84 (Lat/Lon)' },
    { value: 'EPSG:3857',  label: 'Web Mercator' },
];

const COMMODITIES: string[] = [
    'Gold', 'Silver', 'Copper', 'Nickel', 'Zinc', 'Lead',
    'Uranium', 'Lithium', 'Cobalt', 'Iron', 'Tungsten',
    'Molybdenum', 'PGE', 'Diamonds', 'REE', 'Other',
];

interface FileCategory {
    key: string;
    label: string;
    ext: string;
    icon: string;
}

const FILE_CATEGORIES: FileCategory[] = [
    { key: 'collars',   label: 'Drill Collars',   ext: '.csv',      icon: '📍' },
    { key: 'surveys',   label: 'Surveys',          ext: '.csv',      icon: '🧭' },
    { key: 'lithology', label: 'Lithology Logs',   ext: '.csv',      icon: '🪨' },
    { key: 'samples',   label: 'Assay Samples',    ext: '.csv',      icon: '🧪' },
    { key: 'reports',   label: 'NI 43-101 / PDF',  ext: '.pdf',      icon: '📄' },
    { key: 'well_logs', label: 'Well Logs (LAS)',   ext: '.las',      icon: '📊' },
    { key: 'spatial',   label: 'Spatial Data',      ext: '.geojson/.shp', icon: '🗺️' },
    { key: 'excel',     label: 'Excel Workbooks',   ext: '.xlsx',     icon: '📗' },
    { key: 'seismic',   label: 'Seismic (SEG-Y)',   ext: '.sgy',      icon: '🌊' },
    { key: 'xyz',       label: 'XYZ Grids',         ext: '.xyz',      icon: '📐' },
];

interface StepIndicatorProps {
    current: number;
    total: number;
}

function StepIndicator({ current, total }: StepIndicatorProps): JSX.Element {
    return (
        <div className="flex items-center gap-2 mb-6">
            {Array.from({ length: total }, (_, i) => (
                <div key={i} className="flex items-center gap-2">
                    <div
                        className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold border-2 transition-colors ${
                            i < current
                                ? 'bg-amber-600 border-amber-500 text-white'
                                : i === current
                                ? 'border-amber-500 text-amber-400'
                                : 'border-gray-700 text-gray-600'
                        }`}
                    >
                        {i < current ? '✓' : i + 1}
                    </div>
                    {i < total - 1 && (
                        <div className={`w-12 h-0.5 ${i < current ? 'bg-amber-600' : 'bg-gray-700'}`} />
                    )}
                </div>
            ))}
        </div>
    );
}

interface FileUploadCardProps {
    category: FileCategory;
    projectId: string | null;
    token: string | null;
}

function FileUploadCard({ category, projectId, token }: FileUploadCardProps): JSX.Element {
    const [status, setStatus] = useState<'idle' | 'uploading' | 'done' | 'error'>('idle');
    const [fileName, setFileName] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);

    const handleFile = useCallback(async (file: File): Promise<void> => {
        if (!file || !projectId || !token) return;
        setStatus('uploading');
        setFileName(file.name);
        setError(null);

        const formData = new FormData();
        formData.append('file', file);
        formData.append('category', category.key);

        try {
            const res = await fetch(`/api/v1/projects/${projectId}/upload`, {
                method: 'POST',
                headers: {
                    Authorization: `Bearer ${token}`,
                    Accept: 'application/json',
                },
                body: formData,
            });
            if (!res.ok) {
                const body = await res.json();
                throw new Error(body.message || `HTTP ${res.status}`);
            }
            setStatus('done');
        } catch (err) {
            setStatus('error');
            setError(err.message);
        }
    }, [category.key, projectId, token]);

    const handleDrop = (e: React.DragEvent<HTMLLabelElement>): void => {
        e.preventDefault();
        const file = e.dataTransfer?.files?.[0];
        if (file) handleFile(file);
    };

    const handleInput = (e: React.ChangeEvent<HTMLInputElement>): void => {
        const file = e.target.files?.[0];
        if (file) handleFile(file);
    };

    const borderColor =
        status === 'done' ? 'border-green-700' :
        status === 'error' ? 'border-red-700' :
        status === 'uploading' ? 'border-amber-700' :
        'border-gray-700 hover:border-gray-600';

    return (
        <label
            className={`block border ${borderColor} rounded-lg p-3 cursor-pointer transition-colors bg-gray-900/50`}
            onDragOver={(e) => e.preventDefault()}
            onDrop={handleDrop}
        >
            <input type="file" className="sr-only" onChange={handleInput} />
            <div className="flex items-center gap-3">
                <span className="text-lg">{category.icon}</span>
                <div className="flex-1 min-w-0">
                    <div className="text-xs font-medium text-gray-200">{category.label}</div>
                    <div className="text-[10px] text-gray-500 font-mono">{category.ext}</div>
                </div>
                <div className="shrink-0">
                    {status === 'idle' && <span className="text-xs text-gray-500">Drop or click</span>}
                    {status === 'uploading' && <div className="w-4 h-4 rounded-full border-2 border-gray-600 border-t-amber-400 animate-spin" />}
                    {status === 'done' && <span className="text-xs text-green-400">Uploaded</span>}
                    {status === 'error' && <span className="text-xs text-red-400" title={error}>Failed</span>}
                </div>
            </div>
            {fileName && status !== 'idle' && (
                <div className="mt-1 text-[10px] text-gray-500 font-mono truncate">{fileName}</div>
            )}
        </label>
    );
}

interface NewProjectProps {}

interface ProjectForm {
    project_name: string;
    crs_datum: string;
    company: string;
    commodity: string;
    region: string;
    orientation_reference: string;
}

interface CreateProjectApiResponse {
    data?: { project_id?: string };
    project_id?: string;
    message?: string;
}

export default function NewProject(_props: NewProjectProps): JSX.Element {
    const [step, setStep] = useState<number>(0);
    const [form, setForm] = useState<ProjectForm>({
        project_name: '',
        crs_datum: 'EPSG:32613',
        company: '',
        commodity: '',
        region: '',
        orientation_reference: 'BOH',
    });
    const [createdProjectId, setCreatedProjectId] = useState<string | null>(null);
    const [loading, setLoading] = useState<boolean>(false);
    const [error, setError] = useState<string | null>(null);

    function updateField(key: keyof ProjectForm, value: string): void {
        setForm((prev) => ({ ...prev, [key]: value }));
    }

    async function handleCreateProject(e: React.FormEvent<HTMLFormElement>): Promise<void> {
        e.preventDefault();
        setLoading(true);
        setError(null);

        try {
            // Auth via Sanctum session cookie (same-origin). No bearer token from
            // localStorage — localStorage is an XSS-exfiltration target (types.ts:11-12).
            const csrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const res = await fetch('/api/v1/projects', {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    Accept: 'application/json',
                    ...(csrf ? { 'X-CSRF-TOKEN': csrf } : {}),
                },
                body: JSON.stringify(form),
            });

            const data: CreateProjectApiResponse = await res.json();
            if (!res.ok) throw new Error(data.message || `HTTP ${res.status}`);

            const pid = data.data?.project_id || data.project_id;
            setCreatedProjectId(pid);
            setStep(1);
        } catch (err) {
            setError(err.message);
        } finally {
            setLoading(false);
        }
    }

    function handleFinish(): void {
        router.visit('/chat');
    }

    return (
        <AppLayout>
            <Head title="New Project" />
            <div className="flex-1 overflow-y-auto">
                <div className="max-w-2xl mx-auto px-6 py-10">
                    <h1 className="text-2xl font-bold text-gray-100 mb-2">Create New Project</h1>
                    <p className="text-sm text-gray-500 mb-6">
                        Set up a new exploration project and upload your data files.
                    </p>

                    <StepIndicator current={step} total={2} />

                    {/* Step 1: Project metadata */}
                    {step === 0 && (
                        <form onSubmit={handleCreateProject} className="space-y-4">
                            {error && (
                                <div className="text-sm text-red-400 bg-red-950/50 border border-red-800/50 rounded-lg px-3 py-2">
                                    {error}
                                </div>
                            )}

                            <div>
                                <label className="block text-xs text-gray-400 mb-1 font-medium">
                                    Project Name *
                                </label>
                                <input
                                    type="text"
                                    value={form.project_name}
                                    onChange={(e) => updateField('project_name', e.target.value)}
                                    required
                                    className="w-full bg-gray-800 text-gray-100 border border-gray-700 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-amber-500 focus:border-transparent"
                                    placeholder="e.g. Patterson Lake South"
                                />
                            </div>

                            <div className="grid grid-cols-2 gap-4">
                                <div>
                                    <label className="block text-xs text-gray-400 mb-1 font-medium">
                                        Coordinate Reference System
                                    </label>
                                    <select
                                        value={form.crs_datum}
                                        onChange={(e) => updateField('crs_datum', e.target.value)}
                                        className="w-full bg-gray-800 text-gray-100 border border-gray-700 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-amber-500"
                                    >
                                        {CRS_OPTIONS.map((opt) => (
                                            <option key={opt.value} value={opt.value}>
                                                {opt.value} — {opt.label}
                                            </option>
                                        ))}
                                    </select>
                                </div>

                                <div>
                                    <label className="block text-xs text-gray-400 mb-1 font-medium">
                                        Primary Commodity
                                    </label>
                                    <select
                                        value={form.commodity}
                                        onChange={(e) => updateField('commodity', e.target.value)}
                                        className="w-full bg-gray-800 text-gray-100 border border-gray-700 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-amber-500"
                                    >
                                        <option value="">Select commodity</option>
                                        {COMMODITIES.map((c) => (
                                            <option key={c} value={c.toLowerCase()}>{c}</option>
                                        ))}
                                    </select>
                                </div>
                            </div>

                            <div className="grid grid-cols-2 gap-4">
                                <div>
                                    <label className="block text-xs text-gray-400 mb-1 font-medium">Company</label>
                                    <input
                                        type="text"
                                        value={form.company}
                                        onChange={(e) => updateField('company', e.target.value)}
                                        className="w-full bg-gray-800 text-gray-100 border border-gray-700 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-amber-500"
                                        placeholder="e.g. Fission Uranium Corp"
                                    />
                                </div>
                                <div>
                                    <label className="block text-xs text-gray-400 mb-1 font-medium">Region</label>
                                    <input
                                        type="text"
                                        value={form.region}
                                        onChange={(e) => updateField('region', e.target.value)}
                                        className="w-full bg-gray-800 text-gray-100 border border-gray-700 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-amber-500"
                                        placeholder="e.g. Shirley Basin, WY"
                                    />
                                </div>
                            </div>

                            <div>
                                <label className="block text-xs text-gray-400 mb-1 font-medium">
                                    Downhole Orientation Reference
                                </label>
                                <div className="flex gap-4">
                                    {['BOH', 'TOH'].map((ref) => (
                                        <label key={ref} className="flex items-center gap-2 cursor-pointer">
                                            <input
                                                type="radio"
                                                name="orientation_reference"
                                                value={ref}
                                                checked={form.orientation_reference === ref}
                                                onChange={(e) => updateField('orientation_reference', e.target.value)}
                                                className="accent-amber-500"
                                            />
                                            <span className="text-sm text-gray-300 font-mono">{ref}</span>
                                            <span className="text-xs text-gray-500">
                                                ({ref === 'BOH' ? 'Bottom of Hole' : 'Top of Hole'})
                                            </span>
                                        </label>
                                    ))}
                                </div>
                            </div>

                            <div className="pt-4">
                                <button
                                    type="submit"
                                    disabled={loading || !form.project_name.trim()}
                                    className="w-full bg-amber-600 hover:bg-amber-500 disabled:bg-gray-700 disabled:text-gray-500 text-white font-medium rounded-lg py-2.5 text-sm transition-colors"
                                >
                                    {loading ? 'Creating...' : 'Create Project & Continue'}
                                </button>
                            </div>
                        </form>
                    )}

                    {/* Step 2: File upload */}
                    {step === 1 && (
                        <div className="space-y-4">
                            <div className="bg-green-950/40 border border-green-800/40 rounded-lg px-4 py-3 text-sm text-green-300">
                                Project created successfully. Upload your data files below — they'll be processed automatically.
                            </div>

                            <div className="grid grid-cols-2 gap-3">
                                {FILE_CATEGORIES.map((cat) => (
                                    <FileUploadCard
                                        key={cat.key}
                                        category={cat}
                                        projectId={createdProjectId}
                                        token={token}
                                    />
                                ))}
                            </div>

                            <p className="text-xs text-gray-500 text-center mt-2">
                                Files are uploaded to the MinIO bronze bucket. The Dagster pipeline polls every 5 minutes and automatically ingests new uploads.
                            </p>

                            <div className="pt-4 flex gap-3">
                                <button
                                    type="button"
                                    onClick={handleFinish}
                                    className="flex-1 bg-amber-600 hover:bg-amber-500 text-white font-medium rounded-lg py-2.5 text-sm transition-colors"
                                >
                                    Go to Chat
                                </button>
                                <button
                                    type="button"
                                    onClick={() => setStep(0)}
                                    className="px-4 border border-gray-700 text-gray-400 hover:text-gray-200 font-medium rounded-lg py-2.5 text-sm transition-colors"
                                >
                                    Create Another
                                </button>
                            </div>
                        </div>
                    )}
                </div>
            </div>
        </AppLayout>
    );
}
