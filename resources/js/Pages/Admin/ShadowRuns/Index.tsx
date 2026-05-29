import { useMemo, useState } from 'react';
import type { JSX, FormEvent } from 'react';
import { Head, Link, router, useForm } from '@inertiajs/react';
import AppLayout from '../../../Layouts/AppLayout';

/**
 * /admin/shadow-runs — Phase 1 Step 6 Shadow comparison dashboard.
 *
 * Read-mostly admin surface that lets an operator see the dual-write
 * classification distribution, drill into any one row to inspect
 * ``diff_details`` (Show page), and twiddle the traffic-pct flag during
 * the 14-day ramp.
 */

const CLASSIFICATIONS = ['partial', 'clean', 'minor', 'divergent', 'fatal'] as const;
type Classification = (typeof CLASSIFICATIONS)[number];

const CLASS_BADGE: Record<Classification, string> = {
    partial: 'bg-stone-700/40 text-stone-300 border-stone-600',
    clean: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40',
    minor: 'bg-amber-500/15 text-amber-300 border-amber-500/40',
    divergent: 'bg-orange-500/15 text-orange-300 border-orange-500/40',
    fatal: 'bg-red-500/15 text-red-300 border-red-500/40',
};

interface ShadowRun {
    id: string;
    workspace_id: string | null;
    workflow_kind: string;
    classification: Classification;
    minio_key: string;
    correlation_token: string | null;
    v149_duration_ms: number | null;
    hatchet_duration_ms: number | null;
    started_at: string;
    completed_at: string | null;
    has_v149: boolean;
    has_hatchet: boolean;
    has_error: boolean;
}

interface Filters {
    workspace_id: string | null;
    classification: Classification | null;
    workflow_kind: string | null;
    from: string | null;
    to: string | null;
}

interface Summary {
    counts: Record<Classification, number>;
    total_24h: number;
    last_classified_at: string | null;
}

interface TrafficFlag {
    workspace_id: string | null;
    value: number;
}

interface PageProps {
    shadow_runs: ShadowRun[];
    filters: Filters;
    summary: Summary;
    streak: number;
    traffic_flags: TrafficFlag[];
}

function formatDuration(ms: number | null): string {
    if (ms === null) return '—';
    if (ms < 1000) return `${ms} ms`;
    return `${(ms / 1000).toFixed(2)} s`;
}

function formatDate(iso: string | null): string {
    if (iso === null) return '—';
    try {
        return new Date(iso).toLocaleString();
    } catch {
        return iso;
    }
}

function shortId(id: string | null, head = 8): string {
    if (id === null) return '—';
    return id.length > head + 1 ? `${id.slice(0, head)}…` : id;
}

export default function ShadowRunsIndex({
    shadow_runs,
    filters,
    summary,
    streak,
    traffic_flags,
}: PageProps): JSX.Element {
    const [form, setForm] = useState<Filters>(filters);

    const onSubmit = (e: FormEvent<HTMLFormElement>): void => {
        e.preventDefault();
        const data: Record<string, string> = {};
        (Object.entries(form) as [keyof Filters, string | null][]).forEach(([k, v]) => {
            if (v !== null && v !== '') data[k] = v;
        });
        router.get('/admin/shadow-runs', data, { preserveState: true, preserveScroll: true });
    };

    const onReset = (): void => {
        setForm({ workspace_id: null, classification: null, workflow_kind: null, from: null, to: null });
        router.get('/admin/shadow-runs', {}, { preserveState: true, preserveScroll: true });
    };

    const cleanRate24h = useMemo(() => {
        if (summary.total_24h === 0) return null;
        const classified = summary.total_24h - (summary.counts.partial ?? 0);
        if (classified === 0) return null;
        return ((summary.counts.clean / classified) * 100).toFixed(1);
    }, [summary]);

    return (
        <AppLayout>
            <Head title="Shadow runs — Admin" />
            <div className="min-h-screen bg-stone-950 text-stone-100">
                <div className="mx-auto max-w-7xl px-6 py-8" data-testid="shadow-runs-dashboard">
                    <Link href="/dashboard" className="mb-4 inline-block text-sm text-stone-400 hover:text-amber-300">
                        ← Back to dashboard
                    </Link>

                    <header className="mb-6">
                        <h1 className="text-2xl font-semibold text-stone-50">Shadow runs</h1>
                        <p className="mt-1 text-sm text-stone-400">
                            Phase 1 dual-write comparison rows from{' '}
                            <code className="text-stone-300">silver.shadow_runs</code>. Last 200, ordered by
                            <code className="text-stone-300"> started_at DESC</code>.
                        </p>
                    </header>

                    {/* Summary tiles */}
                    <section className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-6">
                        <Tile label="Last 24h" value={String(summary.total_24h)} />
                        {(['clean', 'minor', 'divergent', 'fatal'] as const).map((c) => (
                            <Tile
                                key={c}
                                label={c}
                                value={String(summary.counts[c] ?? 0)}
                                tone={c}
                            />
                        ))}
                        <Tile
                            label="Clean streak (days)"
                            value={String(streak)}
                            tone={streak >= 7 ? 'clean' : 'partial'}
                            hint={cleanRate24h ? `${cleanRate24h}% clean (24h)` : undefined}
                        />
                    </section>

                    {/* Traffic-pct controls */}
                    <TrafficPctEditor flags={traffic_flags} />

                    {/* Filters */}
                    <form
                        onSubmit={onSubmit}
                        className="mb-6 grid grid-cols-1 gap-3 rounded border border-stone-800 bg-stone-900 p-4 md:grid-cols-5"
                    >
                        <FilterInput
                            id="workspace_id"
                            label="Workspace ID"
                            placeholder="uuid"
                            value={form.workspace_id ?? ''}
                            onChange={(v) => setForm({ ...form, workspace_id: v || null })}
                        />
                        <FilterSelect
                            id="classification"
                            label="Classification"
                            value={form.classification ?? ''}
                            options={CLASSIFICATIONS}
                            onChange={(v) =>
                                setForm({ ...form, classification: (v || null) as Classification | null })
                            }
                        />
                        <FilterInput
                            id="workflow_kind"
                            label="Kind"
                            placeholder="ingest_pdf"
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
                                className="rounded bg-amber-500 px-3 py-1.5 text-xs font-medium text-stone-950 hover:bg-amber-400"
                            >
                                Apply filters
                            </button>
                            <button
                                type="button"
                                onClick={onReset}
                                className="rounded border border-stone-700 px-3 py-1.5 text-xs text-stone-300 hover:border-stone-500"
                            >
                                Clear
                            </button>
                        </div>
                    </form>

                    <div className="overflow-x-auto rounded border border-stone-800 bg-stone-900">
                        <table className="w-full text-left text-sm" data-testid="shadow-runs-table">
                            <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                <tr>
                                    <th className="px-3 py-2">Started</th>
                                    <th className="px-3 py-2">Class</th>
                                    <th className="px-3 py-2">Kind</th>
                                    <th className="px-3 py-2">Workspace</th>
                                    <th className="px-3 py-2">Minio key</th>
                                    <th className="px-3 py-2 text-center">v1.49</th>
                                    <th className="px-3 py-2 text-center">Hatchet</th>
                                    <th className="px-3 py-2 text-right">v1.49 ms</th>
                                    <th className="px-3 py-2 text-right">Hatchet ms</th>
                                </tr>
                            </thead>
                            <tbody>
                                {shadow_runs.length === 0 && (
                                    <tr>
                                        <td colSpan={9} className="px-3 py-8 text-center text-stone-500">
                                            No shadow runs match the current filters.
                                        </td>
                                    </tr>
                                )}
                                {shadow_runs.map((row) => (
                                    <tr
                                        key={row.id}
                                        className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30"
                                    >
                                        <td className="whitespace-nowrap px-3 py-2 font-mono text-xs text-stone-300">
                                            <Link
                                                href={`/admin/shadow-runs/${row.id}`}
                                                className="text-amber-300 hover:text-amber-200 hover:underline"
                                            >
                                                {formatDate(row.started_at)}
                                            </Link>
                                        </td>
                                        <td className="px-3 py-2">
                                            <span
                                                className={`inline-block rounded border px-2 py-0.5 text-xs ${CLASS_BADGE[row.classification] ?? CLASS_BADGE.partial}`}
                                            >
                                                {row.classification}
                                            </span>
                                        </td>
                                        <td className="px-3 py-2 text-stone-200">{row.workflow_kind}</td>
                                        <td
                                            className="px-3 py-2 font-mono text-xs text-stone-400"
                                            title={row.workspace_id ?? ''}
                                        >
                                            {shortId(row.workspace_id)}
                                        </td>
                                        <td
                                            className="max-w-md truncate px-3 py-2 font-mono text-xs text-stone-400"
                                            title={row.minio_key}
                                        >
                                            {row.minio_key}
                                        </td>
                                        <td className="px-3 py-2 text-center">{row.has_v149 ? '✓' : '·'}</td>
                                        <td className="px-3 py-2 text-center">{row.has_hatchet ? '✓' : '·'}</td>
                                        <td className="whitespace-nowrap px-3 py-2 text-right font-mono text-xs text-stone-300">
                                            {formatDuration(row.v149_duration_ms)}
                                        </td>
                                        <td className="whitespace-nowrap px-3 py-2 text-right font-mono text-xs text-stone-300">
                                            {formatDuration(row.hatchet_duration_ms)}
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

interface TileProps {
    label: string;
    value: string;
    tone?: Classification;
    hint?: string;
}

function Tile({ label, value, tone = 'partial', hint }: TileProps): JSX.Element {
    return (
        <div className={`rounded border bg-stone-900 p-3 ${CLASS_BADGE[tone]}`}>
            <div className="text-xs uppercase tracking-wide opacity-80">{label}</div>
            <div className="mt-1 text-2xl font-semibold">{value}</div>
            {hint ? <div className="mt-0.5 text-xs opacity-70">{hint}</div> : null}
        </div>
    );
}

function TrafficPctEditor({ flags }: { flags: TrafficFlag[] }): JSX.Element {
    const platform = flags.find((f) => f.workspace_id === null) ?? { workspace_id: null, value: 0 };
    const perWorkspace = flags.filter((f) => f.workspace_id !== null);

    return (
        <section className="mb-6 rounded border border-stone-800 bg-stone-900 p-4">
            <h2 className="text-sm font-semibold text-stone-200">
                ingest_pdf_hatchet_traffic_pct
            </h2>
            <p className="mt-1 text-xs text-stone-400">
                % of incoming PDF uploads that get dual-written to BOTH paths. Ramp 0 → 1 → 10 → 50 → 100
                during the Step 8 cutover window. Workspace-scoped rows override the platform default.
            </p>

            <TrafficRow flag={platform} label="Platform default" />

            {perWorkspace.length > 0 && (
                <details className="mt-3" open>
                    <summary className="cursor-pointer text-xs text-stone-400 hover:text-stone-200">
                        Per-workspace overrides ({perWorkspace.length})
                    </summary>
                    <div className="mt-2 space-y-2">
                        {perWorkspace.map((f) => (
                            <TrafficRow key={f.workspace_id ?? 'platform'} flag={f} label={f.workspace_id!} />
                        ))}
                    </div>
                </details>
            )}
        </section>
    );
}

function TrafficRow({ flag, label }: { flag: TrafficFlag; label: string }): JSX.Element {
    const form = useForm({
        workspace_id: flag.workspace_id ?? '',
        value: flag.value,
    });

    const submit = (e: FormEvent<HTMLFormElement>): void => {
        e.preventDefault();
        form.transform((data) => ({
            workspace_id: data.workspace_id || null,
            value: Number(data.value),
        }));
        form.patch('/admin/shadow-runs/feature-flags/traffic', { preserveScroll: true });
    };

    return (
        <form onSubmit={submit} className="flex flex-wrap items-center gap-2 text-xs">
            <code
                className="max-w-md truncate rounded bg-stone-800 px-2 py-1 font-mono text-stone-300"
                title={label}
            >
                {label}
            </code>
            <input
                type="number"
                min={0}
                max={100}
                step={1}
                value={form.data.value}
                onChange={(e) => form.setData('value', Number(e.target.value))}
                className="w-20 rounded border border-stone-700 bg-stone-800 px-2 py-1 text-right text-sm text-stone-100 focus:border-amber-500 focus:outline-none"
            />
            <span className="text-stone-500">%</span>
            <button
                type="submit"
                disabled={form.processing}
                className="rounded bg-amber-500 px-3 py-1 text-stone-950 hover:bg-amber-400 disabled:opacity-50"
            >
                {form.processing ? 'Saving…' : 'Save'}
            </button>
        </form>
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
                className="rounded border border-stone-700 bg-stone-800 px-2 py-1 text-sm text-stone-100 focus:border-amber-500 focus:outline-none"
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
                className="rounded border border-stone-700 bg-stone-800 px-2 py-1 text-sm text-stone-100 focus:border-amber-500 focus:outline-none"
            >
                <option value="">All</option>
                {options.map((opt) => (
                    <option key={opt} value={opt}>
                        {opt}
                    </option>
                ))}
            </select>
        </label>
    );
}
