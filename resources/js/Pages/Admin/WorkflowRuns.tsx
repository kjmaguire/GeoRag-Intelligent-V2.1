import { useMemo, useState } from 'react';
import type { JSX, FormEvent } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

/**
 * /admin/workflow-runs — Phase 0 Step 3 Workflow Run Dashboard skeleton.
 *
 * Read-only list of the 100 most recent rows from workflow.workflow_runs
 * (the partman-monthly-partitioned table deployed in Phase 0 Step 2).
 * Phase 0 ships the skeleton so operators have visibility the moment any
 * orchestrator (Hatchet / Dagster / Horizon / Kestra / LangGraph)
 * starts writing rows; richer drilldown + replay controls land in Phase 10
 * (Customer Support Cockpit).
 *
 * Backend contract: WorkflowRunController@index renders this page with
 *   { workflow_runs: WorkflowRun[], filters: Filters, tempo_url: string }
 *
 * Filters are server-side query params; the form below issues an
 * Inertia.get() to /admin/workflow-runs, keeping URLs bookmarkable so
 * runbooks can deep-link to e.g. ?status=failure&workflow_kind=ingest_pdf.
 */

const STATUSES = ['queued', 'running', 'success', 'failure', 'cancelled', 'timed_out'] as const;
type Status = (typeof STATUSES)[number];

interface WorkflowRun {
    run_id: string;
    workspace_id: string | null;
    workflow_kind: string;
    engine: string;
    status: Status;
    trace_id: string | null;
    started_at: string;
    ended_at: string | null;
    duration_ms: number | null;
    failure_reason: string | null;
}

interface Filters {
    workspace_id: string | null;
    status: Status | null;
    workflow_kind: string | null;
    from: string | null;
    to: string | null;
}

interface PageProps {
    workflow_runs: WorkflowRun[];
    filters: Filters;
    tempo_url: string;
}

const STATUS_BADGE: Record<Status, string> = {
    queued: 'bg-stone-700/40 text-stone-300 border-stone-600',
    running: 'bg-sky-500/15 text-sky-300 border-sky-500/40',
    success: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40',
    failure: 'bg-red-500/15 text-red-300 border-red-500/40',
    cancelled: 'bg-stone-600/30 text-stone-400 border-stone-600',
    timed_out: 'bg-amber-500/15 text-amber-300 border-amber-500/40',
};

function formatDuration(ms: number | null): string {
    if (ms === null) return '—';
    if (ms < 1000) return `${ms} ms`;
    const sec = ms / 1000;
    if (sec < 60) return `${sec.toFixed(2)} s`;
    const min = Math.floor(sec / 60);
    const remSec = (sec - min * 60).toFixed(0);
    return `${min}m ${remSec}s`;
}

function formatStarted(iso: string): string {
    try {
        const d = new Date(iso);
        return d.toLocaleString(undefined, {
            year: 'numeric',
            month: 'short',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
        });
    } catch {
        return iso;
    }
}

function shortId(id: string | null, head = 8): string {
    if (id === null) return '—';
    return id.length > head + 1 ? `${id.slice(0, head)}…` : id;
}

export default function WorkflowRuns({ workflow_runs, filters, tempo_url }: PageProps): JSX.Element {
    const [form, setForm] = useState<Filters>(filters);

    // Phase 2 real-time push — every named workflow (score_targets,
    // train_target_model, train_source_trust, generate_report,
    // cold_tier_archive, commit_ingestion_run) broadcasts to
    // `admin.workflow-runs` on completion. Partial reload keeps the
    // active filters in the URL bar; the controller re-applies them.
    useAdminSurfaceUpdated('workflow-runs', null, () => {
        router.reload({ only: ['workflow_runs'] });
    });

    const onSubmit = (e: FormEvent<HTMLFormElement>): void => {
        e.preventDefault();
        const data: Record<string, string> = {};
        (Object.entries(form) as [keyof Filters, string | null][]).forEach(([k, v]) => {
            if (v !== null && v !== '') data[k] = v;
        });
        router.get('/admin/workflow-runs', data, { preserveState: true, preserveScroll: true });
    };

    const onReset = (): void => {
        const cleared: Filters = {
            workspace_id: null,
            status: null,
            workflow_kind: null,
            from: null,
            to: null,
        };
        setForm(cleared);
        router.get('/admin/workflow-runs', {}, { preserveState: true, preserveScroll: true });
    };

    const tempoBase = useMemo(() => tempo_url.replace(/\/+$/, ''), [tempo_url]);

    return (
        <AppLayout>
            <Head title="Workflow runs — Admin" />
            <div className="min-h-screen bg-stone-950 text-stone-100">
                <div className="mx-auto max-w-7xl px-6 py-8" data-testid="workflow-runs-dashboard">
                    <Link
                        href="/dashboard"
                        className="mb-4 inline-block text-sm text-stone-400 hover:text-amber-300"
                    >
                        ← Back to dashboard
                    </Link>

                    <header className="mb-6">
                        <h1 className="text-2xl font-semibold text-stone-50">Workflow runs</h1>
                        <p className="mt-1 text-sm text-stone-400">
                            Last {workflow_runs.length} of recent rows from <code className="text-stone-300">workflow.workflow_runs</code>.
                            Click any <code className="text-stone-300">trace_id</code> to open the span tree in Tempo.
                        </p>
                    </header>

                    <form
                        onSubmit={onSubmit}
                        className="mb-6 grid grid-cols-1 gap-3 rounded border border-stone-800 bg-stone-900 p-4 md:grid-cols-5"
                        data-testid="workflow-runs-filters"
                    >
                        <FilterInput
                            id="workspace_id"
                            label="Workspace ID"
                            placeholder="uuid"
                            value={form.workspace_id ?? ''}
                            onChange={(v) => setForm({ ...form, workspace_id: v || null })}
                        />
                        <FilterSelect
                            id="status"
                            label="Status"
                            value={form.status ?? ''}
                            options={STATUSES}
                            onChange={(v) => setForm({ ...form, status: (v || null) as Status | null })}
                        />
                        <FilterInput
                            id="workflow_kind"
                            label="Kind"
                            placeholder="e.g. ingest_pdf"
                            value={form.workflow_kind ?? ''}
                            onChange={(v) => setForm({ ...form, workflow_kind: v || null })}
                        />
                        <FilterInput
                            id="from"
                            label="From"
                            type="datetime-local"
                            value={form.from ?? ''}
                            onChange={(v) => setForm({ ...form, from: v || null })}
                        />
                        <FilterInput
                            id="to"
                            label="To"
                            type="datetime-local"
                            value={form.to ?? ''}
                            onChange={(v) => setForm({ ...form, to: v || null })}
                        />
                        <div className="flex gap-2 md:col-span-5">
                            <button
                                type="submit"
                                className="rounded bg-amber-500 px-3 py-1.5 text-xs font-medium text-stone-950 hover:bg-amber-400 focus:outline-none focus:ring-2 focus:ring-amber-300"
                            >
                                Apply filters
                            </button>
                            <button
                                type="button"
                                onClick={onReset}
                                className="rounded border border-stone-700 px-3 py-1.5 text-xs text-stone-300 hover:border-stone-500 hover:text-stone-100"
                            >
                                Clear
                            </button>
                        </div>
                    </form>

                    <div className="overflow-x-auto rounded border border-stone-800 bg-stone-900">
                        <table className="w-full text-left text-sm" data-testid="workflow-runs-table">
                            <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                <tr>
                                    <th className="px-3 py-2">Started</th>
                                    <th className="px-3 py-2">Kind</th>
                                    <th className="px-3 py-2">Engine</th>
                                    <th className="px-3 py-2">Status</th>
                                    <th className="px-3 py-2">Workspace</th>
                                    <th className="px-3 py-2 text-right">Duration</th>
                                    <th className="px-3 py-2">Trace</th>
                                    <th className="px-3 py-2">Failure</th>
                                </tr>
                            </thead>
                            <tbody>
                                {workflow_runs.length === 0 && (
                                    <tr>
                                        <td colSpan={8} className="px-3 py-8 text-center text-stone-500" data-testid="workflow-runs-empty">
                                            No workflow runs match the current filters.
                                        </td>
                                    </tr>
                                )}
                                {workflow_runs.map((row) => (
                                    <tr
                                        key={row.run_id}
                                        className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30"
                                        data-testid="workflow-run-row"
                                    >
                                        <td className="whitespace-nowrap px-3 py-2 font-mono text-xs text-stone-300">
                                            {formatStarted(row.started_at)}
                                        </td>
                                        <td className="px-3 py-2 text-stone-200">{row.workflow_kind}</td>
                                        <td className="px-3 py-2 text-stone-400">{row.engine}</td>
                                        <td className="px-3 py-2">
                                            <span
                                                className={`inline-block rounded border px-2 py-0.5 text-xs ${STATUS_BADGE[row.status] ?? 'bg-stone-700/40 text-stone-300 border-stone-600'}`}
                                            >
                                                {row.status}
                                            </span>
                                        </td>
                                        <td className="px-3 py-2 font-mono text-xs text-stone-400" title={row.workspace_id ?? ''}>
                                            {shortId(row.workspace_id)}
                                        </td>
                                        <td className="whitespace-nowrap px-3 py-2 text-right font-mono text-xs text-stone-300">
                                            {formatDuration(row.duration_ms)}
                                        </td>
                                        <td className="px-3 py-2 font-mono text-xs">
                                            {row.trace_id ? (
                                                <a
                                                    href={`${tempoBase}/api/traces/${row.trace_id}`}
                                                    target="_blank"
                                                    rel="noopener noreferrer"
                                                    className="text-amber-300 hover:text-amber-200 hover:underline"
                                                    title={row.trace_id}
                                                    data-testid="trace-link"
                                                >
                                                    {shortId(row.trace_id)}
                                                </a>
                                            ) : (
                                                <span className="text-stone-600">—</span>
                                            )}
                                        </td>
                                        <td className="max-w-md px-3 py-2 text-xs text-red-300/90" title={row.failure_reason ?? ''}>
                                            {row.failure_reason ?? <span className="text-stone-600">—</span>}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </AppLayout>
    );
}

interface FilterInputProps {
    id: string;
    label: string;
    value: string;
    onChange: (v: string) => void;
    placeholder?: string;
    type?: string;
}

function FilterInput({ id, label, value, onChange, placeholder, type = 'text' }: FilterInputProps): JSX.Element {
    return (
        <label htmlFor={id} className="flex flex-col gap-1 text-xs text-stone-400">
            {label}
            <input
                id={id}
                name={id}
                type={type}
                value={value}
                placeholder={placeholder}
                onChange={(e) => onChange(e.target.value)}
                className="rounded border border-stone-700 bg-stone-800 px-2 py-1 text-sm text-stone-100 focus:border-amber-500 focus:outline-none focus:ring-1 focus:ring-amber-500"
            />
        </label>
    );
}

interface FilterSelectProps {
    id: string;
    label: string;
    value: string;
    options: readonly string[];
    onChange: (v: string) => void;
}

function FilterSelect({ id, label, value, options, onChange }: FilterSelectProps): JSX.Element {
    return (
        <label htmlFor={id} className="flex flex-col gap-1 text-xs text-stone-400">
            {label}
            <select
                id={id}
                name={id}
                value={value}
                onChange={(e) => onChange(e.target.value)}
                className="rounded border border-stone-700 bg-stone-800 px-2 py-1 text-sm text-stone-100 focus:border-amber-500 focus:outline-none focus:ring-1 focus:ring-amber-500"
            >
                <option value="">All</option>
                {options.map((opt) => (
                    <option key={opt} value={opt}>{opt}</option>
                ))}
            </select>
        </label>
    );
}
