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
    /** Present when the sheet was refused for want of a column mapping. */
    remap?: RemapFacts;
    [key: string]: unknown;
}

/**
 * What a refused sheet needs in order to be re-run with a mapping.
 *
 * Built by ingest_tabular from the parse result, so `columns` is what the
 * parser ACTUALLY saw rather than a list reconstructed from the prose.
 */
interface RemapFacts {
    /** Sheet name, or the filename for a single-table upload. */
    label: string;
    /** collar | survey | lithology | sample. */
    sheet_type: string;
    /** Required fields no column matched. These are what need answering. */
    missing: string[];
    /** Fields that DID match, and to which column. Shown for context. */
    mapped: Record<string, string>;
    /** Every column in the sheet, mapped or not. */
    columns: string[];
}

/** One line of human-readable text for a warning, whatever shape it has. */
function warningText(w: IngestWarning): string {
    if (typeof w.detail === 'string' && w.detail) return w.detail;
    if (typeof w.code === 'string' && w.code) return w.code;
    return JSON.stringify(w);
}

/** `hole_id` → `Hole ID`, for a label a geologist reads rather than parses. */
function fieldLabel(field: string): string {
    return field
        .split('_')
        .map((part) => (part === 'id' ? 'ID' : part.charAt(0).toUpperCase() + part.slice(1)))
        .join(' ');
}

/**
 * Name the columns a refused sheet could not resolve, and re-run it.
 *
 * The refusal message used to end with "rename the key columns to standard
 * names and re-upload" — advice that asks a geologist to edit their source
 * data to suit our vocabulary, and which they cannot follow at all for a
 * file they received from a third party. Worse, the one workaround the
 * message did offer (upload under the matching category) SKIPS the tolerant
 * classifier and lands on the stricter parser, so following it was the
 * surest way to lose the file.
 *
 * Nothing is re-uploaded: the bytes are already in bronze and this
 * re-triggers the same workflow against the same object.
 *
 * Only the MISSING fields get a control. Offering all ten would bury the
 * two that actually need an answer, and the eight that resolved are shown
 * as read-only context so the user can see the parser is not lost.
 */
function ColumnMapper({
    slug,
    minioKey,
    facts,
}: {
    slug: string;
    minioKey: string;
    facts: RemapFacts;
}) {
    const [open, setOpen] = useState(false);
    const [choices, setChoices] = useState<Record<string, string>>({});
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [sent, setSent] = useState(false);

    // Every missing field must be answered: re-running with a partial
    // mapping refuses the file a second time for the fields still unnamed,
    // which reads as "the mapping did not work".
    const answered = facts.missing.filter((f) => (choices[f] ?? '').length > 0);
    const ready = answered.length === facts.missing.length;

    // A column may only stand for one field. Two dropdowns on the same
    // column silently drops one of them at the parser, where a column is
    // claimed once.
    const duplicate = new Set(answered.map((f) => choices[f])).size !== answered.length;

    async function submit() {
        setBusy(true);
        setError(null);
        try {
            const token = document
                .querySelector('meta[name="csrf-token"]')
                ?.getAttribute('content');
            const res = await fetch(`/projects/${slug}/ingestion-runs/remap`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    Accept: 'application/json',
                    ...(token ? { 'X-CSRF-TOKEN': token } : {}),
                },
                body: JSON.stringify({
                    minio_key: minioKey,
                    sheet_type: facts.sheet_type,
                    column_map: choices,
                }),
            });
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                throw new Error(body?.message ?? `Re-run failed (${res.status})`);
            }
            setSent(true);
        } catch (err) {
            setError(err instanceof Error ? err.message : 'Re-run failed');
        } finally {
            setBusy(false);
        }
    }

    if (sent) {
        return (
            <div className="text-[10px] mt-1" style={{ color: 'var(--accent)' }}>
                Re-running “{facts.label}” with your mapping — it will reappear above as
                a new run.
            </div>
        );
    }

    if (!open) {
        return (
            <button
                type="button"
                onClick={() => setOpen(true)}
                className="text-[10px] mt-1 underline underline-offset-2"
                style={{ color: 'var(--accent)' }}
            >
                Map columns for “{facts.label}” instead of renaming them
            </button>
        );
    }

    return (
        <div
            className="mt-2 rounded border p-3 text-[11px]"
            style={{ borderColor: 'var(--line-2)', background: 'var(--bg-2)' }}
        >
            <div className="mb-2" style={{ color: 'var(--fg-1)' }}>
                Which column in <strong>{facts.label}</strong> holds each of these?
            </div>

            <div className="flex flex-col gap-2">
                {facts.missing.map((field) => (
                    <label key={field} className="flex items-center gap-2">
                        <span className="w-28 shrink-0" style={{ color: 'var(--fg-2)' }}>
                            {fieldLabel(field)}
                        </span>
                        <select
                            value={choices[field] ?? ''}
                            onChange={(e) =>
                                setChoices((c) => ({ ...c, [field]: e.target.value }))
                            }
                            className="flex-1 min-w-0 rounded border px-2 py-1"
                            style={{
                                background: 'var(--bg-1)',
                                color: 'var(--fg-0)',
                                borderColor: 'var(--line-2)',
                            }}
                        >
                            <option value="">— choose a column —</option>
                            {facts.columns.map((col) => (
                                <option key={col} value={col}>
                                    {col}
                                </option>
                            ))}
                        </select>
                    </label>
                ))}
            </div>

            {Object.keys(facts.mapped).length > 0 && (
                <div className="mt-2 font-mono text-[10px]" style={{ color: 'var(--fg-3)' }}>
                    Already resolved:{' '}
                    {Object.entries(facts.mapped)
                        .map(([field, col]) => `${fieldLabel(field)} → ${col}`)
                        .join(' · ')}
                </div>
            )}

            {duplicate && (
                <div className="mt-2 text-[10px]" style={{ color: 'var(--warn)' }}>
                    Two fields are pointing at the same column — each column can only
                    stand for one.
                </div>
            )}
            {error && (
                <div className="mt-2 text-[10px]" style={{ color: 'var(--warn)' }}>
                    {error}
                </div>
            )}

            <div className="mt-3 flex items-center gap-2">
                <button
                    type="button"
                    disabled={!ready || duplicate || busy}
                    onClick={submit}
                    className="rounded px-3 py-1 text-[11px] disabled:opacity-40"
                    style={{ background: 'var(--accent)', color: 'var(--bg-0)' }}
                >
                    {busy ? 'Re-running…' : 'Re-run with this mapping'}
                </button>
                <button
                    type="button"
                    onClick={() => setOpen(false)}
                    className="text-[10px] underline underline-offset-2"
                    style={{ color: 'var(--fg-3)' }}
                >
                    Cancel
                </button>
            </div>
        </div>
    );
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
 *
 * A clean 'completed' gets a label too — without one, prettyStage() renders
 * "Completed (step 6 of 6)", a stage report on a run that has no stage any
 * more. These rows are the non-PDF successes (shapefile, drill CSV, LAS)
 * the snapshot now keeps for 24 h; a PDF success lives in the completed
 * card instead and never reaches this list.
 */
function outcomeLabel(row: InFlightRow): string | null {
    if (row.status === 'completed') return 'Completed';
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
    /** pages_with_text / page_count. null on rows ingested before the column existed. */
    text_page_coverage_pct: number | null;
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

    // The list, not the total: totals.in_flight counts only rows still
    // moving, while settled rows (a green shapefile completion, a partial)
    // stay listed for 24 h — a page showing those is not empty, and the
    // empty-state banner must not render above them.
    const empty = runs.in_flight.length === 0 && runs.totals.completed === 0;

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
                        {/* Not "currently ingesting": settled runs — a green
                            shapefile completion, a partial with its warnings,
                            a failure — stay in this card for 24 h so success
                            is visible and problems keep their explanations.
                            The IN FLIGHT stat above counts only moving rows. */}
                        <Card eyebrow={`RUNS · ${runs.in_flight.length}`} title="Active and recent runs" padded={false}>
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
                                                    <div key={i}>
                                                        <div
                                                            className="text-[10px] mt-0.5 truncate"
                                                            style={{ color: 'var(--warn)' }}
                                                            title={warningText(w)}
                                                        >
                                                            {warningText(w)}
                                                        </div>
                                                        {w.remap && (
                                                            <ColumnMapper
                                                                slug={project.slug}
                                                                minioKey={f.key}
                                                                facts={w.remap}
                                                            />
                                                        )}
                                                    </div>
                                                ))}
                                            </div>
                                            <div className="text-[11px]" style={{ color: (f.failed || f.status === 'partial') ? 'var(--warn)' : 'var(--fg-1)' }}>
                                                <Pill tone={(f.failed || f.status === 'partial') ? 'warn' : 'accent'} dot>
                                                    {outcomeLabel(f) ?? prettyStage(f)}
                                                </Pill>
                                                {(f.status === 'partial' || f.status === 'completed') &&
                                                    f.rows_written !== null &&
                                                    f.rows_written > 0 && (
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
                                        className="grid grid-cols-[1fr_90px_100px_100px_110px_150px_110px] text-[10px] font-mono uppercase tracking-wider px-4 py-2 border-b"
                                        style={{ color: 'var(--fg-3)', borderColor: 'var(--line-1)' }}
                                    >
                                        <div>Report</div>
                                        <div>Parser</div>
                                        {/* NOT "Quality". parse_quality_pct is NI 43-101
                                            section-heading coverage; a flawlessly
                                            extracted survey with no numbered sections
                                            scores 0%. "Text pages" beside it is the
                                            extraction number people were reading this
                                            column as. */}
                                        <div>NI 43-101</div>
                                        <div>Text pages</div>
                                        <div>Passages</div>
                                        <div>Embedded</div>
                                        <div>Uploaded</div>
                                    </div>
                                    {runs.completed.map((r) => (
                                        <div
                                            key={r.report_id}
                                            className="grid grid-cols-[1fr_90px_100px_100px_110px_150px_110px] text-xs px-4 py-2.5 border-b items-center"
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
                                            <div className="font-mono" style={{ color: 'var(--fg-2)' }}>
                                                {r.parse_quality_pct === null ? '—' : `${qualityPct(r.parse_quality_pct)}%`}
                                            </div>
                                            <div
                                                className="font-mono"
                                                style={{ color: qualityColor(r.text_page_coverage_pct) }}
                                                title="Fraction of the PDF's pages that produced any text"
                                            >
                                                {r.text_page_coverage_pct === null
                                                    ? '—'
                                                    : `${qualityPct(r.text_page_coverage_pct)}%`}
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

/**
 * Colour for TEXT PAGE COVERAGE, not for NI 43-101 section coverage.
 *
 * It used to tint the section-coverage column, where low is not bad: a
 * 1970s government geophysics survey extracted flawlessly has no numbered
 * sections and scores 0%, and painting that red said "this ingest failed"
 * about a document that had parsed and embedded cleanly. Text page
 * coverage is the number where low genuinely is bad — 0% means no page
 * produced a character — so the colour moved to the column it describes
 * and the section-coverage column is now reported neutrally.
 */
function qualityColor(fraction: number | null): string {
    const pct = qualityPct(fraction);
    if (pct === null) return 'var(--fg-3)';
    if (pct < 10) return 'var(--warn)';
    if (pct < 50) return 'var(--fg-2)';
    return 'var(--accent)';
}
