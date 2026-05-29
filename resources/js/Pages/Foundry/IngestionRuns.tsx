import { Head, Link } from '@inertiajs/react';
import { useEffect, useRef, useState } from 'react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, Stat, EmptyState, ProgressBar } from '@/Components/Foundry/primitives';

/**
 * IngestionRuns — per-project pipeline progress.
 *
 * Phase A: data is derived from silver.reports + bronze MinIO listing on each
 * request. The .json endpoint is polled every 5s while the tab is visible so
 * users can watch a file move from "in flight" to "completed" without having
 * to refresh manually.
 *
 * Phase B will replace the heuristic stage labels ("parsing", "extracting
 * tables", "embedding") with the real per-step status written by each Hatchet
 * step into silver.ingest_progress.
 */

interface InFlightRow {
    key: string;
    filename: string;
    size_bytes: number | null;
    uploaded_at: string | null;
    uploaded_ago: string | null;
    stage: string;
    step_index: number;
    total_steps: number;
    progress_pct: number;
    has_real_progress: boolean;
    failed: boolean;
    error_text: string | null;
}

const STEP_LABELS: Record<string, string> = {
    queued: 'Queued',
    preflight: 'Pre-flight check',
    parse: 'Parsing PDF',
    persist: 'Saving to database',
    embed_verify: 'Verifying embeddings',
    embedding: 'Embedding chunks',
    completed: 'Completed',
    failed: 'Failed',
};

function prettyStage(row: InFlightRow): string {
    const label = STEP_LABELS[row.stage] ?? row.stage;
    if (row.has_real_progress && row.total_steps > 0) {
        return `${label} (step ${row.step_index} of ${row.total_steps})`;
    }
    return label;
}

interface CompletedRow {
    report_id: string;
    title: string;
    parser_used: string | null;
    parse_quality_pct: number | null;
    is_scanned: boolean;
    passages: number;
    embedded: number;
    embed_pct: number;
    uploaded_at: string | null;
    uploaded_ago: string | null;
    filename: string | null;
}

interface RunsSnapshot {
    in_flight: InFlightRow[];
    completed: CompletedRow[];
    totals: { in_flight: number; completed: number };
}

interface IngestionRunsProps {
    project: { project_id: string; project_name: string; slug: string };
    runs: RunsSnapshot;
}

const POLL_INTERVAL_MS = 5000;

function formatBytes(bytes: number | null): string {
    if (bytes === null) return '—';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

/**
 * Reverb event payload — must match
 * App\Events\IngestionProgressBroadcast::broadcastWith().
 */
interface IngestionProgressEvent {
    workspace_id: string;
    project_id: string;
    pipeline_run_id: string;
    stage: string;
    status: 'queued' | 'started' | 'completed' | 'failed' | 'cancelled' | 'timed_out';
    message: string | null;
    pct: number | null;
    timestamp: string;
}

const TERMINAL_STATUSES = ['completed', 'failed', 'cancelled', 'timed_out'] as const;

export default function FoundryIngestionRuns({ project, runs: initial }: IngestionRunsProps) {
    const [runs, setRuns] = useState<RunsSnapshot>(initial);
    const [polling, setPolling] = useState(true);
    const [lastFetched, setLastFetched] = useState<string | null>(null);
    const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    // Snapshot poll — keeps the in-flight / completed lists fresh.
    useEffect(() => {
        if (!polling) return;

        let cancelled = false;

        async function tick(): Promise<void> {
            try {
                const res = await fetch(`/projects/${project.slug}/ingestion-runs.json`, {
                    credentials: 'same-origin',
                    headers: { Accept: 'application/json' },
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const body = await res.json();
                if (cancelled) return;
                setRuns(body.runs);
                setLastFetched(body.fetched_at ?? new Date().toISOString());
            } catch {
                // Swallow — next tick will retry.
            } finally {
                if (!cancelled) {
                    timerRef.current = setTimeout(tick, POLL_INTERVAL_MS);
                }
            }
        }

        timerRef.current = setTimeout(tick, POLL_INTERVAL_MS);

        const onVis = (): void => {
            setPolling(document.visibilityState === 'visible');
        };
        document.addEventListener('visibilitychange', onVis);

        return () => {
            cancelled = true;
            if (timerRef.current) clearTimeout(timerRef.current);
            document.removeEventListener('visibilitychange', onVis);
        };
    }, [polling, project.slug]);

    // Reverb subscription — flips the in-flight list immediately on any
    // ingestion.progress event for this project. The snapshot poll above
    // is still the source of truth for the row layout / completed list;
    // this is just a latency optimisation for terminal-state transitions.
    //
    // Reliability spec Fix 1c + 1e — broadcast fires from on_failure_task,
    // embed_verify (status='completed'), and the stale_run_detector cron.
    useEffect(() => {
        if (typeof window === 'undefined' || !window.Echo) return;

        const channelName = `project.${project.project_id}.ingestion`;
        const ch = window.Echo.private(channelName);

        ch.listen('.ingestion.progress', async (raw: unknown) => {
            const evt = raw as IngestionProgressEvent;
            if (evt.project_id !== project.project_id) return;

            // Terminal event: immediately re-fetch the snapshot so the
            // row jumps from in-flight to completed / failed without
            // waiting for the next 5 s poll tick.
            if ((TERMINAL_STATUSES as readonly string[]).includes(evt.status)) {
                try {
                    const res = await fetch(`/projects/${project.slug}/ingestion-runs.json`, {
                        credentials: 'same-origin',
                        headers: { Accept: 'application/json' },
                    });
                    if (res.ok) {
                        const body = await res.json();
                        setRuns(body.runs);
                        setLastFetched(body.fetched_at ?? new Date().toISOString());
                    }
                } catch {
                    // Best-effort. Snapshot poll will catch up.
                }
            }
        });

        return () => {
            window.Echo?.leave(channelName);
        };
    }, [project.project_id, project.slug]);

    const empty = runs.totals.in_flight === 0 && runs.totals.completed === 0;

    return (
        <AppLayout>
            <Head title={`Ingestion runs · ${project.project_name}`} />

            <div className="flex-1 overflow-y-auto" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · INGESTION RUNS`}
                    title="Pipeline activity"
                    sub={
                        <span>
                            {runs.totals.in_flight} in flight · {runs.totals.completed} completed
                            {lastFetched && (
                                <span style={{ color: 'var(--fg-3)' }}> · refreshed {new Date(lastFetched).toLocaleTimeString()}</span>
                            )}
                        </span>
                    }
                    actions={
                        <Link
                            href={`/projects/${project.slug}/imports/quality`}
                            className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                            style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                        >
                            Trust report →
                        </Link>
                    }
                />

                <section className="grid grid-cols-2 sm:grid-cols-4 gap-px px-8 py-5" style={{ background: 'var(--line-1)' }}>
                    <Stat
                        label="IN FLIGHT"
                        value={String(runs.totals.in_flight)}
                        tone={runs.totals.in_flight > 0 ? 'accent' : undefined}
                        sub={runs.totals.in_flight > 0 ? 'processing now' : 'idle'}
                    />
                    <Stat label="COMPLETED" value={String(runs.totals.completed)} sub="this project" />
                    <Stat
                        label="PASSAGES"
                        value={String(runs.completed.reduce((sum, r) => sum + r.passages, 0))}
                        sub="chunks written"
                    />
                    <Stat
                        label="EMBEDDED"
                        value={String(runs.completed.reduce((sum, r) => sum + r.embedded, 0))}
                        sub="vectors in Qdrant"
                    />
                </section>

                {empty && (
                    <div className="px-8 py-12">
                        <EmptyState
                            title="No ingestion activity for this project yet."
                            detail="Upload a PDF, drill log, or other source on the Data page. As soon as a file lands in MinIO it will show up here, and you can watch it move through parse → tables → embed."
                        />
                    </div>
                )}

                {runs.in_flight.length > 0 && (
                    <section className="px-8 py-5">
                        <Card eyebrow={`IN FLIGHT · ${runs.in_flight.length}`} title="Currently ingesting" padded={false}>
                            <div
                                className="grid grid-cols-[1.4fr_220px_1fr_120px] text-[10px] font-mono uppercase tracking-wider px-4 py-2 border-b"
                                style={{ color: 'var(--fg-3)', borderColor: 'var(--line-1)' }}
                            >
                                <div>File</div>
                                <div>Stage</div>
                                <div>Progress</div>
                                <div>Uploaded</div>
                            </div>
                            {runs.in_flight.map((f) => (
                                <div
                                    key={f.key}
                                    className="grid grid-cols-[1.4fr_220px_1fr_120px] text-xs px-4 py-3 border-b items-center gap-4"
                                    style={{ borderColor: 'var(--line-1)' }}
                                >
                                    <div className="min-w-0">
                                        <div className="truncate" style={{ color: 'var(--fg-0)' }} title={f.filename}>
                                            {f.filename}
                                        </div>
                                        {f.failed && f.error_text && (
                                            <div className="text-[10px] font-mono mt-0.5 truncate" style={{ color: 'var(--warn)' }} title={f.error_text}>
                                                {f.error_text}
                                            </div>
                                        )}
                                    </div>
                                    <div className="text-[11px]" style={{ color: f.failed ? 'var(--warn)' : 'var(--fg-1)' }}>
                                        <Pill tone={f.failed ? 'warn' : 'accent'} dot>
                                            {prettyStage(f)}
                                        </Pill>
                                    </div>
                                    <div className="flex items-center gap-3 min-w-0">
                                        <div className="flex-1 min-w-0">
                                            <ProgressBar
                                                value={f.progress_pct}
                                                tone={f.failed ? 'warn' : (f.progress_pct >= 100 ? 'accent' : 'accent')}
                                                height={6}
                                            />
                                        </div>
                                        <span className="font-mono text-[10px] tabular-nums" style={{ color: 'var(--fg-2)' }}>
                                            {f.progress_pct}%
                                        </span>
                                    </div>
                                    <div className="font-mono text-[11px]" style={{ color: 'var(--fg-3)' }}>
                                        {f.uploaded_ago ?? '—'}
                                    </div>
                                </div>
                            ))}
                        </Card>
                    </section>
                )}

                {runs.completed.length > 0 && (
                    <section className="px-8 py-5 pb-8">
                        <Card eyebrow={`COMPLETED · ${runs.completed.length}`} title="Ingested into silver" padded={false}>
                            <div
                                className="grid grid-cols-[1fr_90px_100px_120px_160px_120px] text-[10px] font-mono uppercase tracking-wider px-4 py-2 border-b"
                                style={{ color: 'var(--fg-3)', borderColor: 'var(--line-1)' }}
                            >
                                <div>Report</div>
                                <div>Parser</div>
                                <div>Quality</div>
                                <div>Passages</div>
                                <div>Embedded</div>
                                <div>Uploaded</div>
                            </div>
                            {runs.completed.map((r) => (
                                <div
                                    key={r.report_id}
                                    className="grid grid-cols-[1fr_90px_100px_120px_160px_120px] text-xs px-4 py-2.5 border-b items-center"
                                    style={{ borderColor: 'var(--line-1)' }}
                                >
                                    <div className="truncate" style={{ color: 'var(--fg-0)' }} title={r.title}>
                                        {r.title}
                                        {r.filename && (
                                            <div className="text-[10px] font-mono mt-0.5 truncate" style={{ color: 'var(--fg-3)' }}>
                                                {r.filename}
                                            </div>
                                        )}
                                    </div>
                                    <div className="font-mono" style={{ color: 'var(--fg-2)' }}>
                                        {r.parser_used ?? '—'}
                                    </div>
                                    <div className="font-mono" style={{ color: qualityColor(r.parse_quality_pct) }}>
                                        {r.parse_quality_pct === null ? '—' : `${Math.round(r.parse_quality_pct)}%`}
                                    </div>
                                    <div className="font-mono" style={{ color: 'var(--fg-1)' }}>
                                        {r.passages.toLocaleString()}
                                    </div>
                                    <div className="flex items-center gap-2">
                                        <div className="flex-1">
                                            <ProgressBar
                                                value={r.embed_pct}
                                                tone={r.embed_pct === 100 ? 'accent' : 'warn'}
                                                height={4}
                                            />
                                        </div>
                                        <span className="font-mono text-[10px]" style={{ color: 'var(--fg-2)' }}>
                                            {r.embedded}/{r.passages}
                                        </span>
                                    </div>
                                    <div className="font-mono" style={{ color: 'var(--fg-3)' }}>
                                        {r.uploaded_ago ?? '—'}
                                    </div>
                                </div>
                            ))}
                        </Card>
                    </section>
                )}
            </div>
        </AppLayout>
    );
}

function qualityColor(pct: number | null): string {
    if (pct === null) return 'var(--fg-3)';
    if (pct < 10) return 'var(--warn)';
    if (pct < 50) return 'var(--fg-2)';
    return 'var(--accent)';
}
