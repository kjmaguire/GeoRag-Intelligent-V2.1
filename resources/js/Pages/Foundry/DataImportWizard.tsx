import { useEffect, useRef, useState } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill } from '@/Components/Foundry/primitives';

/**
 * Foundry / DataImportWizard
 *
 * Real upload + redirect-to-runs.
 *
 * Step 1 ("Drop") — user picks a target project (fetched from /api/v1/projects)
 *                   and drops one or more files.
 * Step 2 ("Submit") — files are POSTed to /api/v1/projects/{id}/upload.
 *                     On success we router.visit to the project's IngestionRuns
 *                     page, which is the canonical surface for watching ingest
 *                     progress (Echo + 5 s poll fallback).
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

interface UploadOutcome {
    filename: string;
    ok: boolean;
    message: string;
}

export default function FoundryDataImportWizard() {
    const [projects, setProjects] = useState<ProjectPick[] | null>(null);
    const [projectsError, setProjectsError] = useState<string | null>(null);
    const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null);
    const [files, setFiles] = useState<File[]>([]);
    const [submitting, setSubmitting] = useState(false);
    const [outcomes, setOutcomes] = useState<UploadOutcome[]>([]);
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

    function handleFilesPicked(picked: FileList | null) {
        if (!picked) return;
        setFiles(Array.from(picked));
        setOutcomes([]);
    }

    function csrfHeader(): Record<string, string> {
        const token =
            document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') ?? null;
        return token ? { 'X-CSRF-TOKEN': token } : {};
    }

    async function uploadOne(projectId: string, file: File): Promise<UploadOutcome> {
        const fd = new FormData();
        fd.append('file', file);
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
                    filename: file.name,
                    ok: false,
                    message: body.message ?? `HTTP ${res.status}`,
                };
            }
            return { filename: file.name, ok: true, message: 'queued' };
        } catch (err) {
            return {
                filename: file.name,
                ok: false,
                message: err instanceof Error ? err.message : String(err),
            };
        }
    }

    async function handleSubmit() {
        if (!selectedProject || files.length === 0) return;
        setSubmitting(true);
        setOutcomes([]);

        // Upload sequentially so per-file failures don't break the whole
        // batch — UploadController validates size + content-type per file
        // and we want clear per-file feedback rather than a single 4xx.
        const results: UploadOutcome[] = [];
        for (const f of files) {
            const r = await uploadOne(selectedProject.project_id, f);
            results.push(r);
            setOutcomes([...results]);
        }

        setSubmitting(false);

        const anyOk = results.some((r) => r.ok);
        if (anyOk) {
            // Hand off to the page that actually shows live ingest progress.
            router.visit(`/projects/${selectedProject.slug}/ingestion-runs`);
        }
    }

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
                            className="h-44 rounded-md border-2 border-dashed flex flex-col items-center justify-center"
                            style={{ borderColor: 'var(--line-2)', background: 'var(--bg-2)' }}
                        >
                            <div className="text-sm font-medium mb-1" style={{ color: 'var(--fg-1)' }}>
                                Drop PDF / LAS / CSV / SEG-Y / AGS / KMZ here
                            </div>
                            <div
                                className="text-[11px] font-mono uppercase tracking-wider mb-3"
                                style={{ color: 'var(--fg-3)' }}
                            >
                                or use the file picker
                            </div>
                            <input
                                ref={fileInputRef}
                                type="file"
                                multiple
                                className="hidden"
                                onChange={(e) => handleFilesPicked(e.target.files)}
                            />
                            <button
                                type="button"
                                onClick={() => fileInputRef.current?.click()}
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

                        {files.length > 0 && (
                            <div className="mt-4">
                                <div
                                    className="text-[10px] font-mono uppercase tracking-[0.12em] mb-2"
                                    style={{ color: 'var(--fg-3)' }}
                                >
                                    Selected · {files.length}
                                </div>
                                <ul className="text-xs space-y-1">
                                    {files.map((f) => {
                                        const outcome = outcomes.find(
                                            (o) => o.filename === f.name,
                                        );
                                        return (
                                            <li
                                                key={f.name}
                                                className="flex items-center gap-3"
                                                style={{ color: 'var(--fg-1)' }}
                                            >
                                                <span className="font-mono">{f.name}</span>
                                                <span
                                                    className="font-mono"
                                                    style={{ color: 'var(--fg-3)' }}
                                                >
                                                    {(f.size / 1024).toFixed(1)} KB
                                                </span>
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
                                            </li>
                                        );
                                    })}
                                </ul>
                            </div>
                        )}
                    </Card>

                    <footer className="flex justify-end gap-2">
                        <button
                            type="button"
                            disabled={!selectedProject || files.length === 0 || submitting}
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
