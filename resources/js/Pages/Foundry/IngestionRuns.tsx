import { Head, Link } from '@inertiajs/react';
import { useEffect, useRef, useState } from 'react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, Stat, EmptyState, ProgressBar } from '@/Components/Foundry/primitives';
import { formatTime } from '@/lib/time';
import { listenPrivate } from '@/lib/echoChannel';

/**
 * IngestionRuns — per-project pipeline progress.
 *
 * Phase A: data is derived from silver.reports + a bronze object-storage
 * listing (STORAGE_BACKEND-agnostic — SeaweedFS/MinIO or Azure Blob) on each
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
    stage_detail?: string | null;
    step_index: number;
    total_steps: number;
    progress_pct: number;
    has_real_progress: boolean;
    failed: boolean;
    error_text: string | null;
    /**
     * Terminal state from silver.ingest_progress. 'partial' means the run
     * reached the end and still needs attention — it wrote nothing, or it
     * wrote something and also raised warnings.
     */
    status: string;
    /** Rows the run actually wrote. null when the workflow does not report it. */
    rows_written: number | null;
    /** Diagnostics the workflow collected, e.g. no_matching_collar. */
    warnings: IngestWarning[];
}

interface IngestWarning {
    code?: string;
    detail?: string;
    [key: string]: unknown;
}

/** One line of human-readable text for a warning, whatever shape it has. */
function warningText(w: IngestWarning): string {
    if (typeof w.detail === 'string' && w.detail) return w.detail;
    if (typeof w.code === 'string' && w.code) return w.code;
    return JSON.stringify(w);
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

/**
 * A run that finished but produced nothing is not a success, and saying
 * "Completed" over it is how a lost upload stays lost. The label leads with
 * the outcome the user can act on.
 */
function outcomeLabel(row: InFlightRow): string | null {
    if (row.status !== 'partial') return null;
    if (row.rows_written === 0) return 'Finished — no data written';
    return 'Finished with warnings';
}

function prettyStage(row: InFlightRow): string {
    const label = STEP_LABELS[row.stage] ?? row.stage;
    // Page-level detail from the worker ("OCR page 61/210") beats the
    // step counter when present.
    if (row.stage_detail) {
        return `${label} — ${row.stage_detail}`;
    }
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
// Perf audit 2026-08-15 (item 4) — same in_flight-gated backoff as
// Overview.tsx's ingest-summary poll: hitting the .json endpoint every 5s
// forever, even on a project with nothing in flight, is wasted load. Widen
// to 30s once there's nothing left to watch move; the Reverb subscription
// below still flips `runs` immediately on the next real ingestion event,
// so idle projects don't lose responsiveness, just poll less.
const POLL_BACKOFF_MS = 30000;

// How many consecutive failed polls before the page admits it is stale.
//
// Two, not one. A single dropped request between two good ones is ordinary
// -- a laptop waking, a redeploy rolling a pod -- and a banner that appears
// and vanishes teaches people to ignore it. Two in a row at the current
// interval means 10s of failure with something in flight, or a minute when
// idle, which is long enough to be real.
const STALE_AFTER_FAILURES = 2;

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

// Mirrors _progress.TERMINAL_STATUSES on the FastAPI side. 'partial' was
// missing, so the one terminal outcome that carries a warning worth
// reading was also the one that did not trigger the immediate re-fetch —
// it sat in the in-flight list until the next poll tick.
const TERMINAL_STATUSES = ['completed', 'partial', 'failed', 'cancelled', 'timed_out'] as const;

export default function FoundryIngestionRuns({ project, runs: initial }: IngestionRunsProps) {
    const [runs, setRuns] = useState<RunsSnapshot>(initial);
    const [polling, setPolling] = useState(true);
    const [lastFetched, setLastFetched] = useState<string | null>(null);
    // Consecutive failed polls. The catch below used to swallow every
    // error, so a session expiry or a backend outage left this page
    // rendering its last good snapshot indefinitely while looking
    // healthy -- on the one screen whose entire purpose is telling you
    // whether something is still moving.
    const [failedPolls, setFailedPolls] = useState(0);
    const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    // Tab visibility — its own always-mounted effect. This listener used to
    // live inside the polling effect below, AFTER an early `if (!polling)
    // return` — so the moment the tab was hidden (polling → false), the
    // effect re-ran, bailed before registering the listener, and nothing
    // could ever flip polling back to true. Registering it unconditionally
    // means hide → show now resumes the poll.
    useEffect(() => {
        const onVis = (): void => {
            setPolling(document.visibilityState === 'visible');
        };
        document.addEventListener('visibilitychange', onVis);
        return () => document.removeEventListener('visibilitychange', onVis);
    }, []);

    // Snapshot poll — keeps the in-flight / completed lists fresh. Gated on
    // `polling` (tab visible); the visibility effect above re-arms it.
    useEffect(() => {
        if (!polling) return;

        let cancelled = false;

        async function tick(): Promise<void> {
            try {
                const res = await fetch(`/projects/${project.slug}/ingestion-runs.json`, {
                    credentials: 'same-origin',
                    headers: { Accept: 'application/json' },
                });

                // 401 (unauthenticated) and 419 (expired CSRF token)
                // cannot recover by waiting -- every later request gets
                // the same answer. SESSION_LIFETIME is 120 minutes, so
                // any page left open through lunch lands here. Retrying
                // forever is how the frozen-but-healthy-looking page
                // happened.
                if (res.status === 401 || res.status === 419) {
                    window.location.href = '/login';
                    return;
                }

                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const body = await res.json();
                if (cancelled) return;
                setRuns(body.runs);
                setLastFetched(body.fetched_at ?? new Date().toISOString());
                // Clear on success so the banner cannot outlive the
                // problem it describes.
                setFailedPolls(0);
            } catch {
                // Counted, not swallowed. The banner needs two failures
                // before it appears: one dropped request between two
                // good ones is ordinary on a laptop, and a warning that
                // flickers is a warning people learn to ignore.
                if (!cancelled) setFailedPolls((n) => n + 1);
            } finally {
                if (!cancelled) {
                    timerRef.current = setTimeout(
                        tick,
                        runs.totals.in_flight > 0 ? POLL_INTERVAL_MS : POLL_BACKOFF_MS,
                    );
                }
            }
        }

        timerRef.current = setTimeout(
            tick,
            runs.totals.in_flight > 0 ? POLL_INTERVAL_MS : POLL_BACKOFF_MS,
        );

        return () => {
            cancelled = true;
            if (timerRef.current) clearTimeout(timerRef.current);
        };
    }, [polling, project.slug, runs.totals.in_flight]);

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

        // Ref-counted — the shell's ingest-toast bridge subscribes to this
        // same channel from the persistent layout, and the bare
        // Echo.leave() that used to be here unbound it on the way out.
        const unsubscribe = listenPrivate(channelName, '.ingestion.progress', async (raw) => {
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
            unsubscribe();
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
                                <span style={{ color: 'var(--fg-3)' }}> · refreshed {formatTime(lastFetched)}</span>
                            )}
                            {failedPolls >= STALE_AFTER_FAILURES && (
                                <span style={{ color: 'var(--warn, #d97706)' }}>
                                    {' · '}
                                    live updates paused
                                    {lastFetched
                                        ? ` — last updated ${formatTime(lastFetched)}`
                                        : ''}
                                    {' '}
                                    <button
                                        type="button"
                                        onClick={() => setFailedPolls(0)}
                                        className="underline"
                                        style={{ color: 'inherit' }}
                                    >
                                        Retry
                                    </button>
                                </span>
                            )}
                        </span>
                    }
                    actions={
                        <Link
                            href={`/projects/${project.slug}/reports`}
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
                            detail="Upload a PDF, drill log, or other source and it will show up here, where you can watch it move through parse → tables → embed."
                            action={
                                <Link
                                    href="/foundry/imports/wizard"
                                    className="inline-block text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                    style={{
                                        color: 'var(--accent)',
                                        background: 'var(--accent-bg)',
                                        borderColor: 'var(--accent-dim)',
                                    }}
                                >
                                    ↑ Upload files →
                                </Link>
                            }
                        />
                    </div>
                )}

                {runs.in_flight.length > 0 && (
                    <section className="px-8 py-5">
                        <Card eyebrow={`IN FLIGHT · ${runs.in_flight.length}`} title="Currently ingesting" padded={false}>
                            {/* Fixed-px columns don't collapse below `lg:` — scroll
                                horizontally instead of clipping/overlapping on narrow
                                viewports. */}
                            <div className="overflow-x-auto">
                                <div className="min-w-[640px]">
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
                                                {(f.warnings ?? []).map((w, i) => (
                                                    <div
                                                        key={i}
                                                        className="text-[10px] mt-0.5 truncate"
                                                        style={{ color: 'var(--warn)' }}
                                                        title={warningText(w)}
                                                    >
                                                        {warningText(w)}
                                                    </div>
                                                ))}
                                            </div>
                                            <div className="text-[11px]" style={{ color: (f.failed || f.status === 'partial') ? 'var(--warn)' : 'var(--fg-1)' }}>
                                                <Pill tone={(f.failed || f.status === 'partial') ? 'warn' : 'accent'} dot>
                                                    {outcomeLabel(f) ?? prettyStage(f)}
                                                </Pill>
                                                {f.status === 'partial' && f.rows_written !== null && f.rows_written > 0 && (
                                                    <span className="ml-2 font-mono text-[10px] tabular-nums" style={{ color: 'var(--fg-2)' }}>
                                                        {f.rows_written.toLocaleString()} rows
                                                    </span>
                                                )}
                                            </div>
                                            <div className="flex items-center gap-3 min-w-0">
                                                <div className="flex-1 min-w-0">
                                                    <ProgressBar
                                                        value={f.progress_pct}
                                                        tone={(f.failed || f.status === 'partial') ? 'warn' : 'accent'}
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
                                </div>
                            </div>
                        </Card>
                    </section>
                )}

                {runs.completed.length > 0 && (
                    <section className="px-8 py-5 pb-8">
                        <Card eyebrow={`COMPLETED · ${runs.completed.length}`} title="Ingested into silver" padded={false}>
                            {/* Fixed-px columns don't collapse below `lg:` — scroll
                                horizontally instead of clipping/overlapping on narrow
                                viewports. */}
                            <div className="overflow-x-auto">
                                <div className="min-w-[720px]">
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
                                                {r.parse_quality_pct === null ? '—' : `${qualityPct(r.parse_quality_pct)}%`}
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
                                </div>
                            </div>
                        </Card>
                    </section>
                )}
            </div>
        </AppLayout>
    );
}

/**
 * silver.reports.parse_quality_pct is stored as a FRACTION, not a 0-100
 * percentage, despite the column name: pdf_report.py computes it as
 * `unique_numbered_sections / NI43_BASELINE_SECTIONS` (17). Live values run
 * 0.29–1.71. Rendering it directly as `${Math.round(v)}%` therefore showed
 * every document as "0%"/"1%"/"2%" in warning red, which read as a total
 * ingestion failure on documents that had parsed and embedded cleanly.
 *
 * It can also legitimately exceed 1.0 when a report carries more numbered
 * sections than the 17-section baseline, so cap the display at 100%.
 */
function qualityPct(fraction: number | null): number | null {
    return fraction === null ? null : Math.min(100, Math.round(fraction * 100));
}

function qualityColor(fraction: number | null): string {
    const pct = qualityPct(fraction);
    if (pct === null) return 'var(--fg-3)';
    if (pct < 10) return 'var(--warn)';
    if (pct < 50) return 'var(--fg-2)';
    return 'var(--accent)';
}
