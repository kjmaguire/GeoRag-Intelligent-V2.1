import type { JSX } from 'react';
import { Head, Link } from '@inertiajs/react';
import AppLayout from '../../../Layouts/AppLayout';

/**
 * /admin/shadow-runs/{id} — Phase 1 Step 6 drill-down for a single
 * silver.shadow_runs row. Renders:
 *
 *   - top metadata strip
 *   - per-side timing + audit_run_id
 *   - the full diff_details.checks list (one per row, colour-coded by ok)
 *   - raw v149_result + hatchet_result JSON for direct inspection
 */

const CLASS_BADGE: Record<string, string> = {
    partial: 'bg-stone-700/40 text-stone-300 border-stone-600',
    clean: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40',
    minor: 'bg-amber-500/15 text-amber-300 border-amber-500/40',
    divergent: 'bg-orange-500/15 text-orange-300 border-orange-500/40',
    fatal: 'bg-red-500/15 text-red-300 border-red-500/40',
};

interface DiffCheck {
    check: string;
    ok?: boolean;
    informational?: boolean;
    [k: string]: unknown;
}

interface DiffDetails {
    checks?: DiffCheck[];
}

interface ShadowRun {
    id: string;
    workspace_id: string | null;
    workflow_kind: string;
    classification: string;
    minio_key: string;
    correlation_token: string | null;
    v149_duration_ms: number | null;
    hatchet_duration_ms: number | null;
    v149_audit_run_id: string | null;
    hatchet_audit_run_id: string | null;
    started_at: string;
    completed_at: string | null;
    error_v149: string | null;
    error_hatchet: string | null;
    v149_result: Record<string, unknown> | null;
    hatchet_result: Record<string, unknown> | null;
    diff_details: DiffDetails | null;
}

interface PageProps {
    shadow_run: ShadowRun;
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

export default function ShadowRunsShow({ shadow_run }: PageProps): JSX.Element {
    const checks = shadow_run.diff_details?.checks ?? [];
    const failed = checks.filter((c) => c.ok === false && !c.informational);
    const passed = checks.filter((c) => c.ok !== false || c.informational);

    return (
        <AppLayout>
            <Head title={`Shadow run ${shadow_run.id.slice(0, 8)} — Admin`} />
            <div className="min-h-screen bg-stone-950 text-stone-100">
                <div className="mx-auto max-w-7xl px-6 py-8">
                    <Link
                        href="/admin/shadow-runs"
                        className="mb-4 inline-block text-sm text-stone-400 hover:text-amber-300"
                    >
                        ← All shadow runs
                    </Link>

                    <header className="mb-6 flex flex-wrap items-baseline gap-3">
                        <h1 className="text-2xl font-semibold text-stone-50">
                            Shadow run <code className="text-amber-300">{shadow_run.id.slice(0, 8)}</code>
                        </h1>
                        <span
                            className={`rounded border px-2 py-0.5 text-xs ${CLASS_BADGE[shadow_run.classification] ?? CLASS_BADGE.partial}`}
                        >
                            {shadow_run.classification}
                        </span>
                        <span className="font-mono text-xs text-stone-500">{shadow_run.workflow_kind}</span>
                    </header>

                    {/* Metadata strip */}
                    <section className="mb-6 grid grid-cols-1 gap-3 rounded border border-stone-800 bg-stone-900 p-4 md:grid-cols-2">
                        <Field label="Workspace" value={shadow_run.workspace_id} mono />
                        <Field label="Correlation token" value={shadow_run.correlation_token} mono />
                        <Field label="Minio key" value={shadow_run.minio_key} mono />
                        <Field label="Started" value={formatDate(shadow_run.started_at)} />
                        <Field label="Completed" value={formatDate(shadow_run.completed_at)} />
                        <Field
                            label="v1.49 / Hatchet duration"
                            value={`${formatDuration(shadow_run.v149_duration_ms)} / ${formatDuration(
                                shadow_run.hatchet_duration_ms,
                            )}`}
                        />
                        <Field label="v1.49 audit run_id" value={shadow_run.v149_audit_run_id} mono />
                        <Field label="Hatchet audit run_id" value={shadow_run.hatchet_audit_run_id} mono />
                    </section>

                    {(shadow_run.error_v149 || shadow_run.error_hatchet) && (
                        <section className="mb-6 grid grid-cols-1 gap-3 md:grid-cols-2">
                            {shadow_run.error_v149 && (
                                <ErrorCard label="error_v149" message={shadow_run.error_v149} />
                            )}
                            {shadow_run.error_hatchet && (
                                <ErrorCard label="error_hatchet" message={shadow_run.error_hatchet} />
                            )}
                        </section>
                    )}

                    {/* Diff checks */}
                    <section className="mb-6 rounded border border-stone-800 bg-stone-900 p-4">
                        <h2 className="mb-3 text-sm font-semibold text-stone-200">
                            Diff checks ({failed.length} failed / {passed.length} passed)
                        </h2>
                        {checks.length === 0 ? (
                            <p className="text-sm text-stone-500">
                                No diff_details yet — row is still <code>partial</code> or pre-Step 5B.
                            </p>
                        ) : (
                            <ul className="space-y-1.5">
                                {[...failed, ...passed].map((c, i) => (
                                    <li
                                        key={`${c.check}-${i}`}
                                        className={`rounded border px-3 py-2 text-xs font-mono ${
                                            c.ok === false && !c.informational
                                                ? 'border-orange-500/40 bg-orange-500/5 text-orange-200'
                                                : c.informational
                                                  ? 'border-stone-700 bg-stone-800/50 text-stone-400'
                                                  : 'border-emerald-500/30 bg-emerald-500/5 text-emerald-300'
                                        }`}
                                    >
                                        <div className="flex flex-wrap items-baseline gap-2">
                                            <span className="font-semibold">{c.check}</span>
                                            {c.informational ? (
                                                <span className="text-stone-500">(informational)</span>
                                            ) : c.ok === false ? (
                                                <span>FAIL</span>
                                            ) : (
                                                <span>OK</span>
                                            )}
                                        </div>
                                        <pre className="mt-1 whitespace-pre-wrap text-xs opacity-80">
                                            {JSON.stringify(
                                                Object.fromEntries(
                                                    Object.entries(c).filter(
                                                        ([k]) => k !== 'check' && k !== 'ok' && k !== 'informational',
                                                    ),
                                                ),
                                                null,
                                                2,
                                            )}
                                        </pre>
                                    </li>
                                ))}
                            </ul>
                        )}
                    </section>

                    {/* Raw payloads */}
                    <section className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                        <RawJsonCard label="v149_result" value={shadow_run.v149_result} />
                        <RawJsonCard label="hatchet_result" value={shadow_run.hatchet_result} />
                    </section>
                </div>
            </div>
        </AppLayout>
    );
}

function Field({
    label,
    value,
    mono = false,
}: {
    label: string;
    value: string | null;
    mono?: boolean;
}): JSX.Element {
    return (
        <div>
            <div className="text-xs uppercase tracking-wide text-stone-500">{label}</div>
            <div
                className={`mt-0.5 break-all text-sm text-stone-200 ${mono ? 'font-mono text-xs' : ''}`}
                title={value ?? ''}
            >
                {value ?? '—'}
            </div>
        </div>
    );
}

function ErrorCard({ label, message }: { label: string; message: string }): JSX.Element {
    return (
        <div className="rounded border border-red-500/40 bg-red-500/5 p-3">
            <div className="text-xs uppercase tracking-wide text-red-300">{label}</div>
            <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap text-xs text-red-200">{message}</pre>
        </div>
    );
}

function RawJsonCard({ label, value }: { label: string; value: unknown }): JSX.Element {
    return (
        <div className="rounded border border-stone-800 bg-stone-900 p-3">
            <div className="mb-2 text-xs uppercase tracking-wide text-stone-400">{label}</div>
            {value === null ? (
                <p className="text-xs text-stone-500">— not yet populated —</p>
            ) : (
                <pre className="max-h-96 overflow-auto whitespace-pre-wrap text-xs text-stone-300">
                    {JSON.stringify(value, null, 2)}
                </pre>
            )}
        </div>
    );
}
