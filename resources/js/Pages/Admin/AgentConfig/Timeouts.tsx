import { useState } from 'react';
import type { JSX, FormEvent } from 'react';
import { Head, Link, router, usePage } from '@inertiajs/react';
import AppLayout from '../../../Layouts/AppLayout';

/**
 * /admin/agent-config/timeouts — Phase 0 Step 5.2.
 *
 * Lists rows of workspace.agent_timeouts. Inline edit per row dispatches
 * a PATCH /admin/agent-config/timeouts/{agent_name} which writes the row
 * + an audit_ledger entry inside one DB transaction.
 *
 * Backend contract: TimeoutsController@index renders this page with
 *   { timeouts: AgentTimeout[] }
 * Flash data: the controller sets ?->with('success', ...) on save.
 */

const SCOPES = ['none', 'workspace', 'global'] as const;
type Scope = (typeof SCOPES)[number];

interface AgentTimeout {
    agent_name: string;
    risk_tier: string;
    soft_timeout_ms: number;
    hard_timeout_ms: number;
    retry_count: number;
    circuit_breaker_scope: Scope;
    failure_threshold: number;
    cool_down_seconds: number;
    updated_at: string | null;
    updated_by: number | null;
}

interface PageProps {
    timeouts: AgentTimeout[];
    [key: string]: unknown;
}

interface FlashProps {
    flash?: { success?: string };
    [key: string]: unknown;
}

export default function Timeouts({ timeouts }: PageProps): JSX.Element {
    const { props } = usePage<FlashProps>();
    const flash = props.flash?.success ?? null;

    return (
        <AppLayout>
            <Head title="Agent timeouts — Admin" />
            <div className="min-h-screen bg-stone-950 text-stone-100">
                <div className="mx-auto max-w-7xl px-6 py-8" data-testid="agent-config-timeouts">
                    <Link
                        href="/dashboard"
                        className="mb-4 inline-block text-sm text-stone-400 hover:text-amber-300"
                    >
                        ← Back to dashboard
                    </Link>

                    <header className="mb-6">
                        <h1 className="text-2xl font-semibold text-stone-50">Agent timeouts</h1>
                        <p className="mt-1 text-sm text-stone-400">
                            Per-agent soft/hard timeout, retry, and circuit-breaker policy from{' '}
                            <code className="text-stone-300">workspace.agent_timeouts</code>. Every change writes an
                            audit_ledger entry under <code className="text-stone-300">workspace.agent_timeouts.update</code>.
                        </p>
                    </header>

                    {flash && (
                        <div
                            className="mb-4 rounded border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300"
                            data-testid="flash-success"
                        >
                            {flash}
                        </div>
                    )}

                    <div className="overflow-x-auto rounded border border-stone-800 bg-stone-900">
                        <table className="w-full text-left text-sm" data-testid="timeouts-table">
                            <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                <tr>
                                    <th className="px-3 py-2">Agent</th>
                                    <th className="px-3 py-2">Tier</th>
                                    <th className="px-3 py-2 text-right">Soft (ms)</th>
                                    <th className="px-3 py-2 text-right">Hard (ms)</th>
                                    <th className="px-3 py-2 text-right">Retries</th>
                                    <th className="px-3 py-2">CB scope</th>
                                    <th className="px-3 py-2 text-right">Threshold</th>
                                    <th className="px-3 py-2 text-right">Cool-down (s)</th>
                                    <th className="px-3 py-2"></th>
                                </tr>
                            </thead>
                            <tbody>
                                {timeouts.length === 0 && (
                                    <tr>
                                        <td colSpan={9} className="px-3 py-8 text-center text-stone-500">
                                            No agent timeouts seeded.
                                        </td>
                                    </tr>
                                )}
                                {timeouts.map((row) => (
                                    <Row key={row.agent_name} row={row} />
                                ))}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </AppLayout>
    );
}

function Row({ row }: { row: AgentTimeout }): JSX.Element {
    const [form, setForm] = useState({
        soft_timeout_ms: row.soft_timeout_ms,
        hard_timeout_ms: row.hard_timeout_ms,
        retry_count: row.retry_count,
        circuit_breaker_scope: row.circuit_breaker_scope,
        failure_threshold: row.failure_threshold,
        cool_down_seconds: row.cool_down_seconds,
    });
    const [saving, setSaving] = useState(false);

    const onSubmit = (e: FormEvent<HTMLFormElement>): void => {
        e.preventDefault();
        setSaving(true);
        router.patch(
            `/admin/agent-config/timeouts/${encodeURIComponent(row.agent_name)}`,
            form,
            {
                preserveScroll: true,
                onFinish: () => setSaving(false),
            },
        );
    };

    return (
        <tr className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30" data-testid="timeout-row">
            <td className="px-3 py-2 text-stone-200">{row.agent_name}</td>
            <td className="px-3 py-2 font-mono text-xs text-stone-400">{row.risk_tier}</td>
            <td className="px-3 py-2 text-right">
                <NumInput
                    value={form.soft_timeout_ms}
                    min={1}
                    max={600000}
                    onChange={(v) => setForm({ ...form, soft_timeout_ms: v })}
                />
            </td>
            <td className="px-3 py-2 text-right">
                <NumInput
                    value={form.hard_timeout_ms}
                    min={1}
                    max={600000}
                    onChange={(v) => setForm({ ...form, hard_timeout_ms: v })}
                />
            </td>
            <td className="px-3 py-2 text-right">
                <NumInput
                    value={form.retry_count}
                    min={0}
                    max={10}
                    onChange={(v) => setForm({ ...form, retry_count: v })}
                />
            </td>
            <td className="px-3 py-2">
                <select
                    value={form.circuit_breaker_scope}
                    onChange={(e) =>
                        setForm({ ...form, circuit_breaker_scope: e.target.value as Scope })
                    }
                    className="rounded border border-stone-700 bg-stone-800 px-2 py-1 text-sm text-stone-100"
                >
                    {SCOPES.map((s) => (
                        <option key={s} value={s}>
                            {s}
                        </option>
                    ))}
                </select>
            </td>
            <td className="px-3 py-2 text-right">
                <NumInput
                    value={form.failure_threshold}
                    min={1}
                    max={1000}
                    onChange={(v) => setForm({ ...form, failure_threshold: v })}
                />
            </td>
            <td className="px-3 py-2 text-right">
                <NumInput
                    value={form.cool_down_seconds}
                    min={0}
                    max={86400}
                    onChange={(v) => setForm({ ...form, cool_down_seconds: v })}
                />
            </td>
            <td className="px-3 py-2 text-right">
                <form onSubmit={onSubmit}>
                    <button
                        type="submit"
                        disabled={saving}
                        className="rounded bg-amber-500 px-3 py-1 text-xs font-medium text-stone-950 hover:bg-amber-400 disabled:opacity-60"
                        data-testid="save-button"
                    >
                        {saving ? 'Saving…' : 'Save'}
                    </button>
                </form>
            </td>
        </tr>
    );
}

function NumInput({
    value,
    onChange,
    min,
    max,
}: {
    value: number;
    onChange: (v: number) => void;
    min?: number;
    max?: number;
}): JSX.Element {
    return (
        <input
            type="number"
            value={value}
            min={min}
            max={max}
            onChange={(e) => onChange(Number(e.target.value))}
            className="w-24 rounded border border-stone-700 bg-stone-800 px-2 py-1 text-right text-sm text-stone-100 focus:border-amber-500 focus:outline-none"
        />
    );
}
