import type { JSX, FormEvent } from 'react';
import { Head, Link, router, useForm } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

/**
 * /admin/integrations — Phase 2 Step 6 Kestra dashboard.
 *
 * One row per registered Kestra-driven Hatchet workflow:
 *   - flow_name + kind + description
 *   - feature-flag toggle (activepieces.<flow>.enabled)
 *   - last 24h Hatchet run rollup (completed/failed/running/queued)
 *   - last 24h audit emissions (validates the workflow actually wrote)
 *
 * Plus:
 *   - Kestra-side flow list (what the operator wired up in the UI)
 *   - Recent flag-flip history (R-P1-6 sidecar)
 */

interface FlowRow {
    flow_name: string;
    flag_name: string;
    kind: string;
    description: string;
    enabled: boolean;
    flag_updated_at: string | null;
    last_24h: {
        completed: number;
        failed: number;
        running: number;
        queued: number;
        cancelled: number;
        p50_duration_ms: number | null;
        p95_duration_ms: number | null;
        last_started_at: string | null;
    };
    audit_emissions_24h: number;
}

interface KestraFlow {
    id: string;
    namespace: string;
    revision: number | null;
    created: string;
    updated: string;
}

interface FlagHistoryRow {
    op: string;
    flag_name: string;
    old_value: boolean | null;
    new_value: boolean | null;
    actor_id: number | null;
    changed_at: string;
}

interface SenderRow {
    id: string;
    source: string;
    secret_kid: string;
    created_at: string;
    last_seen_at: string | null;
    disabled_at: string | null;
    receive_count_24h: number;
}

interface FlowJwtKeyRow {
    flow_name: string;
    kid: string;
    valid_from: string;
    valid_until: string | null;
    is_active: boolean;
    created_at: string;
}

interface RotationHistoryRow {
    action_type: string;
    created_at: string;
    actor_id: number | null;
    flow_name: string | null;
    source: string | null;
    prior_kid: string | null;
    new_kid: string | null;
    overlap_hours: number | null;
}

interface PageProps {
    flows: FlowRow[];
    kestra_flows: KestraFlow[];
    flow_history: FlagHistoryRow[];
    senders: SenderRow[];
    flow_jwt_keys: FlowJwtKeyRow[];
    rotation_history: RotationHistoryRow[];
    new_sender_secret: string | null;
    new_sender_source: string | null;
}

function formatDate(iso: string | null): string {
    if (iso === null) return '—';
    try {
        return new Date(iso).toLocaleString();
    } catch {
        return iso;
    }
}

function formatDurationMs(ms: number | null): string {
    if (ms === null || ms === undefined) return '—';
    if (ms < 1000) return `${ms} ms`;
    if (ms < 60_000) return `${(ms / 1000).toFixed(2)} s`;
    return `${(ms / 60_000).toFixed(1)} m`;
}

function relativeAgo(iso: string | null): string {
    if (iso === null) return '—';
    try {
        const sec = (Date.now() - new Date(iso).getTime()) / 1000;
        if (sec < 60) return `${Math.round(sec)}s ago`;
        if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
        if (sec < 86_400) return `${Math.round(sec / 3600)}h ago`;
        return `${Math.round(sec / 86_400)}d ago`;
    } catch {
        return '—';
    }
}

export default function Integrations({
    flows,
    kestra_flows,
    flow_history,
    senders,
    flow_jwt_keys,
    rotation_history,
    new_sender_secret,
    new_sender_source,
}: PageProps): JSX.Element {
    // Phase 5 real-time push — flow_jwt_key_reaper broadcasts `integrations`
    // on every rotation cron run. Refreshes the rotation_history table + the
    // JWT-keys list; flag_history and senders update on user-driven CRUD so
    // they stay on the existing router.reload paths.
    useAdminSurfaceUpdated('integrations', null, () => {
        router.reload({ only: ['flow_history', 'rotation_history', 'flow_jwt_keys'] });
    });

    return (
        <AppLayout>
            <Head title="Integrations — Admin" />
            <div className="min-h-screen bg-stone-950 text-stone-100">
                <div className="mx-auto max-w-7xl px-6 py-8" data-testid="integrations-dashboard">
                    <Link href="/dashboard" className="mb-4 inline-block text-sm text-stone-400 hover:text-amber-300">
                        ← Back to dashboard
                    </Link>

                    <header className="mb-6 flex flex-wrap items-baseline gap-3">
                        <h1 className="text-2xl font-semibold text-stone-50">Integrations</h1>
                        <a
                            href="/admin/integrations/kestra/"
                            target="_blank"
                            rel="noopener noreferrer"
                            className="ml-auto rounded border border-amber-500/40 bg-amber-500/10 px-3 py-1 text-xs text-amber-300 hover:bg-amber-500/20"
                        >
                            Open Kestra UI →
                        </a>
                        <p className="mt-1 w-full text-sm text-stone-400">
                            Kestra-driven Hatchet workflows. Phase 3 — Kestra is the
                            integration edge for external feeds + inbound webhooks. Toggle a flow
                            to start/stop the receiving side immediately; the Kestra side
                            keeps running unless disabled in Kestra's own UI (link above —
                            Sanctum-gated, no second password).
                        </p>
                    </header>

                    {/* Flow rows */}
                    <section className="mb-8 grid grid-cols-1 gap-3">
                        {flows.length === 0 && (
                            <div className="rounded border border-stone-800 bg-stone-900 p-6 text-center text-stone-500">
                                No Kestra flows are registered.
                            </div>
                        )}
                        {flows.map((f) => (
                            <FlowCard key={f.flow_name} flow={f} />
                        ))}
                    </section>

                    {/* Senders (Phase 4 Step 5) — per-sender HMAC registry */}
                    <section className="mb-8 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            External notification senders ({senders.length})
                        </h2>
                        {senders.length === 0 ? (
                            <p className="px-4 py-4 text-xs text-stone-500">
                                No senders registered yet. Run{' '}
                                <code className="text-stone-300">scripts/phase4_sender_register.sh add &lt;source&gt;</code>{' '}
                                to add one — the env-var fallback covers single-sender deployments.
                            </p>
                        ) : (
                            <div className="overflow-x-auto">
                                <table className="w-full text-left text-sm">
                                    <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                        <tr>
                                            <th className="px-3 py-2">Source</th>
                                            <th className="px-3 py-2">Key id</th>
                                            <th className="px-3 py-2 text-right">Receives 24h</th>
                                            <th className="px-3 py-2">Last seen</th>
                                            <th className="px-3 py-2">Status</th>
                                            <th className="px-3 py-2"></th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {senders.map((s) => (
                                            <SenderRowView key={s.id} sender={s} />
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        )}
                    </section>

                    {/* Phase 10 Step 3 — new-sender secret banner (one-shot). */}
                    {new_sender_secret && new_sender_source && (
                        <section className="mb-4 rounded border border-emerald-500/40 bg-emerald-500/10 p-4 text-sm text-emerald-200">
                            <div className="mb-2 font-semibold">
                                Copy this secret now — it's only shown once.
                            </div>
                            <div className="mb-2 text-xs text-emerald-300">
                                Source: <code className="text-emerald-100">{new_sender_source}</code>
                            </div>
                            <code className="block break-all rounded border border-emerald-700 bg-stone-950 p-2 font-mono text-xs text-emerald-100">
                                {new_sender_secret}
                            </code>
                            <p className="mt-2 text-xs text-emerald-400">
                                Paste this into the sender's HMAC secret config.
                                Refreshing this page will clear the secret —
                                we don't store it anywhere recoverable.
                            </p>
                        </section>
                    )}

                    {/* Phase 10 Step 3 — register-sender form. */}
                    <RegisterSenderForm />

                    {/* Per-flow JWT keys (Phase 6 Step 3 + Phase 8 Step 2) */}
                    <section className="mb-8 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Per-flow JWT keys ({flow_jwt_keys.length})
                        </h2>
                        {flow_jwt_keys.length === 0 ? (
                            <p className="px-4 py-4 text-xs text-stone-500">
                                No per-flow JWT keys provisioned. The env-var fallback
                                (<code className="text-stone-300">KESTRA_FLOW_JWT_SECRET</code>) still
                                signs and verifies tokens. Run{' '}
                                <code className="text-stone-300">scripts/phase3_jwt_rotate.sh provision-key &lt;flow&gt; &lt;kid&gt;</code>
                                {' '}to provision a per-flow key.
                            </p>
                        ) : (
                            <div className="overflow-x-auto">
                                <table className="w-full text-left text-sm">
                                    <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                        <tr>
                                            <th className="px-3 py-2">Flow</th>
                                            <th className="px-3 py-2">Kid</th>
                                            <th className="px-3 py-2">Valid from</th>
                                            <th className="px-3 py-2">Valid until</th>
                                            <th className="px-3 py-2">Status</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {flow_jwt_keys.map((k) => (
                                            <tr
                                                key={`${k.flow_name}/${k.kid}`}
                                                className="border-b border-stone-800/60 last:border-b-0"
                                            >
                                                <td className="px-3 py-2 font-mono text-xs text-stone-200">{k.flow_name}</td>
                                                <td className="px-3 py-2 font-mono text-xs text-stone-300">{k.kid}</td>
                                                <td className="px-3 py-2 text-xs text-stone-400">{relativeAgo(k.valid_from)}</td>
                                                <td className="px-3 py-2 text-xs text-stone-400">
                                                    {k.valid_until === null ? (
                                                        <span className="text-emerald-400">open</span>
                                                    ) : (
                                                        relativeAgo(k.valid_until)
                                                    )}
                                                </td>
                                                <td className="px-3 py-2 text-xs">
                                                    {k.is_active ? (
                                                        <span className="rounded bg-emerald-900/40 px-2 py-0.5 text-emerald-300">active</span>
                                                    ) : (
                                                        <span className="rounded bg-stone-800 px-2 py-0.5 text-stone-500">expired</span>
                                                    )}
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        )}
                        {/* Phase 9 Step 2 (R-P8-1) — rotate-with-overlap form. */}
                        <RotateFlowKeyForm flows={flows} />
                    </section>

                    {/* Kestra-side flow list (Phase 3 — primary integration edge) */}
                    <section className="mb-8 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Kestra flows ({kestra_flows.length})
                        </h2>
                        {kestra_flows.length === 0 ? (
                            <p className="px-4 py-4 text-xs text-stone-500">
                                No flows registered in Kestra yet. YAML files are at
                                <code className="ml-1">kestra/flows/georag/*.yaml</code> — load them
                                via the Kestra UI or
                                <code className="mx-1">kestra flow update</code>.
                            </p>
                        ) : (
                            <div className="overflow-x-auto">
                                <table className="w-full text-left text-sm">
                                    <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                        <tr>
                                            <th className="px-3 py-2">Namespace</th>
                                            <th className="px-3 py-2">Flow ID</th>
                                            <th className="px-3 py-2 text-right">Revision</th>
                                            <th className="px-3 py-2">Updated</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {kestra_flows.map((kf) => (
                                            <tr
                                                key={`${kf.namespace}/${kf.id}`}
                                                className="border-b border-stone-800/60 last:border-b-0"
                                            >
                                                <td className="px-3 py-2 font-mono text-xs text-stone-400">{kf.namespace}</td>
                                                <td className="px-3 py-2 font-mono text-xs text-stone-200">{kf.id}</td>
                                                <td className="px-3 py-2 text-right font-mono text-xs text-stone-300">
                                                    {kf.revision ?? '—'}
                                                </td>
                                                <td className="px-3 py-2 text-xs text-stone-400">{relativeAgo(kf.updated)}</td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        )}
                    </section>

                    {/* Phase 12 Step 4 (R-P10-2) — Rotation history. */}
                    <section className="mb-8 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Rotation history ({rotation_history.length})
                        </h2>
                        {rotation_history.length === 0 ? (
                            <p className="px-4 py-4 text-xs text-stone-500">
                                No JWT or HMAC rotations recorded yet. The first rotation
                                via the Rotate buttons will land here.
                            </p>
                        ) : (
                            <div className="overflow-x-auto">
                                <table className="w-full text-left text-sm">
                                    <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                        <tr>
                                            <th className="px-3 py-2">When</th>
                                            <th className="px-3 py-2">Kind</th>
                                            <th className="px-3 py-2">Target</th>
                                            <th className="px-3 py-2">Prior → New kid</th>
                                            <th className="px-3 py-2">Actor</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {rotation_history.map((r, i) => (
                                            <tr
                                                key={`${r.action_type}-${r.created_at}-${i}`}
                                                className="border-b border-stone-800/60 last:border-b-0"
                                            >
                                                <td className="px-3 py-2 text-xs text-stone-400">{relativeAgo(r.created_at)}</td>
                                                <td className="px-3 py-2 text-xs">
                                                    {r.action_type === 'workflow.jwt_key.rotated' ? (
                                                        <span className="rounded border border-sky-500/40 px-2 py-0.5 text-sky-300">JWT</span>
                                                    ) : (
                                                        <span className="rounded border border-fuchsia-500/40 px-2 py-0.5 text-fuchsia-300">HMAC</span>
                                                    )}
                                                </td>
                                                <td className="px-3 py-2 font-mono text-xs text-stone-200">
                                                    {r.flow_name ?? r.source ?? '—'}
                                                </td>
                                                <td className="px-3 py-2 font-mono text-xs text-stone-300">
                                                    {r.prior_kid ?? '—'} → {r.new_kid ?? '—'}
                                                    {r.overlap_hours !== null && (
                                                        <span className="ml-2 text-stone-500">(overlap {r.overlap_hours}h)</span>
                                                    )}
                                                </td>
                                                <td className="px-3 py-2 text-xs text-stone-400">
                                                    {r.actor_id !== null ? `#${r.actor_id}` : 'system'}
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        )}
                    </section>

                    {/* Flag history */}
                    <section className="rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Recent flag flips
                        </h2>
                        {flow_history.length === 0 ? (
                            <p className="px-4 py-4 text-xs text-stone-500">No flag history yet.</p>
                        ) : (
                            <div className="overflow-x-auto">
                                <table className="w-full text-left text-sm">
                                    <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                        <tr>
                                            <th className="px-3 py-2">When</th>
                                            <th className="px-3 py-2">Op</th>
                                            <th className="px-3 py-2">Flag</th>
                                            <th className="px-3 py-2">Old → New</th>
                                            <th className="px-3 py-2">Actor</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {flow_history.map((h, i) => (
                                            <tr
                                                key={`${h.flag_name}-${h.changed_at}-${i}`}
                                                className="border-b border-stone-800/60 last:border-b-0"
                                            >
                                                <td className="px-3 py-2 text-xs text-stone-400">
                                                    {relativeAgo(h.changed_at)}
                                                </td>
                                                <td className="px-3 py-2">
                                                    <span className="rounded border border-stone-700 bg-stone-800 px-2 py-0.5 text-xs text-stone-300">
                                                        {h.op}
                                                    </span>
                                                </td>
                                                <td className="px-3 py-2 font-mono text-xs text-stone-200">
                                                    {h.flag_name}
                                                </td>
                                                <td className="px-3 py-2 font-mono text-xs">
                                                    <span className="text-stone-500">
                                                        {h.old_value === null ? '—' : String(h.old_value)}
                                                    </span>
                                                    <span className="mx-2 text-stone-500">→</span>
                                                    <span className={h.new_value ? 'text-emerald-300' : 'text-stone-300'}>
                                                        {h.new_value === null ? '—' : String(h.new_value)}
                                                    </span>
                                                </td>
                                                <td className="px-3 py-2 font-mono text-xs text-stone-400">
                                                    {h.actor_id !== null ? `user#${h.actor_id}` : 'system'}
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        )}
                    </section>
                </div>
            </div>
        </AppLayout>
    );
}

function FlowCard({ flow }: { flow: FlowRow }): JSX.Element {
    const form = useForm({ value: flow.enabled });

    const submit = (e: FormEvent<HTMLFormElement>): void => {
        e.preventDefault();
        const next = !flow.enabled;
        form.transform(() => ({ value: next }));
        form.patch(`/admin/integrations/flags/${flow.flag_name}`, {
            preserveScroll: true,
            onSuccess: () => router.reload({ only: ['flows', 'flow_history'] }),
        });
    };

    const totalRuns =
        flow.last_24h.completed +
        flow.last_24h.failed +
        flow.last_24h.running +
        flow.last_24h.queued +
        flow.last_24h.cancelled;

    return (
        <article className="rounded border border-stone-800 bg-stone-900 p-4">
            <header className="mb-3 flex flex-wrap items-baseline gap-3">
                <code className="text-base font-semibold text-amber-300">{flow.flow_name}</code>
                <span className="rounded border border-stone-700 bg-stone-800 px-2 py-0.5 text-xs text-stone-300">
                    {flow.kind}
                </span>
                <form onSubmit={submit} className="ml-auto">
                    <button
                        type="submit"
                        disabled={form.processing}
                        className={`rounded border px-3 py-1 text-xs font-medium transition ${
                            flow.enabled
                                ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25'
                                : 'border-stone-600 bg-stone-700/30 text-stone-300 hover:bg-stone-700/50'
                        }`}
                    >
                        {form.processing ? 'Saving…' : flow.enabled ? '● Enabled' : '○ Disabled'}
                    </button>
                </form>
            </header>

            <p className="mb-3 text-sm text-stone-400">{flow.description}</p>

            <div className="grid grid-cols-2 gap-3 text-xs md:grid-cols-7">
                <Stat label="Last 24h runs" value={String(totalRuns)} />
                <Stat label="Completed" value={String(flow.last_24h.completed)} tone="good" />
                <Stat
                    label="Failed"
                    value={String(flow.last_24h.failed)}
                    tone={flow.last_24h.failed > 0 ? 'bad' : 'neutral'}
                />
                <Stat label="Running" value={String(flow.last_24h.running)} />
                <Stat label="p50" value={formatDurationMs(flow.last_24h.p50_duration_ms)} />
                <Stat label="p95" value={formatDurationMs(flow.last_24h.p95_duration_ms)} />
                <Stat label="Audit rows" value={String(flow.audit_emissions_24h)} />
            </div>
            <p className="mt-2 text-xs text-stone-500">
                Last started {relativeAgo(flow.last_24h.last_started_at)}
            </p>

            {flow.flag_updated_at && (
                <p className="mt-3 text-xs text-stone-500">
                    Flag last changed {relativeAgo(flow.flag_updated_at)} ({formatDate(flow.flag_updated_at)})
                </p>
            )}
        </article>
    );
}

function SenderRowView({ sender }: { sender: SenderRow }): JSX.Element {
    const disabled = sender.disabled_at !== null;
    const action = disabled ? 'enable' : 'disable';
    const toggleForm = useForm({});
    const rotateForm = useForm({});

    const submitToggle = (e: FormEvent<HTMLFormElement>): void => {
        e.preventDefault();
        toggleForm.patch(`/admin/integrations/senders/${sender.id}/${action}`, {
            preserveScroll: true,
            onSuccess: () => router.reload({ only: ['senders', 'flow_history', 'rotation_history'] }),
        });
    };

    const submitRotate = (e: FormEvent<HTMLFormElement>): void => {
        e.preventDefault();
        rotateForm.post(`/admin/integrations/senders/${sender.id}/rotate-hmac`, {
            preserveScroll: true,
            onSuccess: () =>
                router.reload({
                    only: [
                        'senders',
                        'rotation_history',
                        'new_sender_secret',
                        'new_sender_source',
                    ],
                }),
        });
    };

    return (
        <tr className="border-b border-stone-800/60 last:border-b-0">
            <td className="px-3 py-2 font-mono text-xs text-stone-200">{sender.source}</td>
            <td className="px-3 py-2 font-mono text-xs text-stone-400">{sender.secret_kid}</td>
            <td className="px-3 py-2 text-right text-stone-300">{sender.receive_count_24h}</td>
            <td className="px-3 py-2 text-xs text-stone-400">{relativeAgo(sender.last_seen_at)}</td>
            <td className="px-3 py-2">
                <span
                    className={`rounded border px-2 py-0.5 text-xs ${
                        disabled
                            ? 'border-stone-600 bg-stone-700/30 text-stone-400'
                            : 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300'
                    }`}
                >
                    {disabled ? 'disabled' : 'active'}
                </span>
            </td>
            <td className="px-3 py-2">
                <div className="flex items-center gap-2">
                    <form onSubmit={submitToggle}>
                        <button
                            type="submit"
                            disabled={toggleForm.processing}
                            className={`rounded border px-2 py-0.5 text-xs ${
                                disabled
                                    ? 'border-emerald-500/40 text-emerald-300 hover:bg-emerald-500/15'
                                    : 'border-amber-500/40 text-amber-300 hover:bg-amber-500/15'
                            } disabled:opacity-50`}
                        >
                            {toggleForm.processing ? '…' : disabled ? 'Enable' : 'Disable'}
                        </button>
                    </form>
                    {!disabled && (
                        <form onSubmit={submitRotate}>
                            <button
                                type="submit"
                                disabled={rotateForm.processing}
                                className="rounded border border-sky-500/40 px-2 py-0.5 text-xs text-sky-300 hover:bg-sky-500/15 disabled:opacity-50"
                                title="Rotate this sender's HMAC secret"
                            >
                                {rotateForm.processing ? 'Rotating…' : 'Rotate HMAC'}
                            </button>
                        </form>
                    )}
                </div>
            </td>
        </tr>
    );
}

function RegisterSenderForm(): JSX.Element {
    const form = useForm({ source: '', description: '' });

    const submit = (e: FormEvent<HTMLFormElement>): void => {
        e.preventDefault();
        form.post('/admin/integrations/senders', {
            preserveScroll: true,
            onSuccess: () => {
                form.reset();
                router.reload({ only: ['senders', 'new_sender_secret', 'new_sender_source'] });
            },
        });
    };

    return (
        <section className="mb-8 rounded border border-stone-800 bg-stone-900">
            <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                Register a new sender
            </h2>
            <form
                onSubmit={submit}
                className="flex flex-wrap items-end gap-3 px-4 py-3 text-xs"
            >
                <label className="flex flex-col text-stone-400">
                    Source
                    <input
                        type="text"
                        placeholder="e.g. ops_pager"
                        className="mt-1 w-56 rounded border border-stone-700 bg-stone-950 px-2 py-1 font-mono text-xs text-stone-200"
                        value={form.data.source}
                        onChange={(e): void => form.setData('source', e.target.value)}
                        required
                    />
                </label>
                <label className="flex flex-col text-stone-400">
                    Description (optional)
                    <input
                        type="text"
                        placeholder="What this sender feeds us"
                        className="mt-1 w-80 rounded border border-stone-700 bg-stone-950 px-2 py-1 text-xs text-stone-200"
                        value={form.data.description}
                        onChange={(e): void => form.setData('description', e.target.value)}
                        maxLength={255}
                    />
                </label>
                <button
                    type="submit"
                    disabled={form.processing || form.data.source.trim() === ''}
                    className="rounded border border-emerald-500/40 bg-emerald-500/15 px-3 py-1.5 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/25 disabled:opacity-50"
                >
                    {form.processing ? 'Registering…' : 'Register sender'}
                </button>
                {form.errors.source && (
                    <p className="text-xs text-red-400">{form.errors.source}</p>
                )}
                {form.errors.description && (
                    <p className="text-xs text-red-400">{form.errors.description}</p>
                )}
            </form>
            <p className="border-t border-stone-800 px-4 py-2 text-xs text-stone-500">
                The fresh HMAC secret is shown ONCE on the banner above
                after submission — copy it before refreshing. Source must
                match <code className="text-stone-400">^[a-z][a-z0-9_-]&#123;1,63&#125;$</code>.
            </p>
        </section>
    );
}

function RotateFlowKeyForm({ flows }: { flows: FlowRow[] }): JSX.Element {
    const form = useForm({
        flow_name: flows[0]?.flow_name ?? '',
        overlap_hours: 24,
    });

    const submit = (e: FormEvent<HTMLFormElement>): void => {
        e.preventDefault();
        form.post('/admin/integrations/jwt-keys/rotate', {
            preserveScroll: true,
            onSuccess: () =>
                router.reload({ only: ['flow_jwt_keys'] }),
        });
    };

    if (flows.length === 0) {
        return (
            <p className="border-t border-stone-800 px-4 py-3 text-xs text-stone-500">
                No registered flows to rotate keys for.
            </p>
        );
    }

    return (
        <form
            onSubmit={submit}
            className="flex flex-wrap items-end gap-3 border-t border-stone-800 px-4 py-3 text-xs"
        >
            <label className="flex flex-col text-stone-400">
                Flow
                <select
                    className="mt-1 rounded border border-stone-700 bg-stone-950 px-2 py-1 font-mono text-xs text-stone-200"
                    value={form.data.flow_name}
                    onChange={(e): void => form.setData('flow_name', e.target.value)}
                >
                    {flows.map((f) => (
                        <option key={f.flow_name} value={f.flow_name}>
                            {f.flow_name}
                        </option>
                    ))}
                </select>
            </label>
            <label className="flex flex-col text-stone-400">
                Overlap (hours)
                <input
                    type="number"
                    min={0}
                    max={168}
                    className="mt-1 w-24 rounded border border-stone-700 bg-stone-950 px-2 py-1 font-mono text-xs text-stone-200"
                    value={form.data.overlap_hours}
                    onChange={(e): void =>
                        form.setData('overlap_hours', Number(e.target.value) || 0)
                    }
                />
            </label>
            <button
                type="submit"
                disabled={form.processing}
                className="rounded border border-emerald-500/40 bg-emerald-500/15 px-3 py-1.5 text-xs font-semibold text-emerald-300 hover:bg-emerald-500/25 disabled:opacity-50"
            >
                {form.processing ? 'Rotating…' : 'Rotate with overlap'}
            </button>
            {form.errors.flow_name && (
                <p className="text-xs text-red-400">{form.errors.flow_name}</p>
            )}
            {form.errors.overlap_hours && (
                <p className="text-xs text-red-400">{form.errors.overlap_hours}</p>
            )}
        </form>
    );
}

function Stat({
    label,
    value,
    tone = 'neutral',
}: {
    label: string;
    value: string;
    tone?: 'good' | 'bad' | 'neutral';
}): JSX.Element {
    const colour =
        tone === 'good'
            ? 'text-emerald-300'
            : tone === 'bad'
              ? 'text-red-300'
              : 'text-stone-200';
    return (
        <div className="rounded border border-stone-800 bg-stone-800/30 p-2">
            <div className="text-stone-500">{label}</div>
            <div className={`mt-0.5 text-base font-semibold ${colour}`}>{value}</div>
        </div>
    );
}
