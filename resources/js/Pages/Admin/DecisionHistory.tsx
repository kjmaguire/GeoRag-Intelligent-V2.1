import type { JSX } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

/**
 * /admin/decision-history — Master-plan §9.12 Decision History view
 * (doc-phase 129).
 *
 * Cross-workspace read-only view of silver.decision_records + the
 * audit.audit_ledger entries the writer (`record_decision`) anchors.
 *
 * Backend: app/Http/Controllers/Admin/DecisionHistoryController.php.
 */

interface KPIs {
    total_decisions: number;
    decisions_with_audit_anchor: number;
    audit_anchor_pct: number;
    mean_uncertainty: number | null;
    distinct_workspaces: number;
    distinct_deciders: number;
    recent_30d_count: number;
    latest_decided_at: string | null;
}

interface ByDecisionType {
    decision_type: string;
    total: number;
    accepted: number;
    modified: number;
    rejected: number;
    signed_off: number;
    other: number;
}

interface ByHumanDecision {
    human_decision: string;
    count: number;
}

interface DecisionRow {
    decision_id: string;
    workspace_id: string;
    decision_type: string;
    recommendation: string;
    human_decision: string;
    uncertainty: number | null;
    has_audit_anchor: boolean;
    decided_at: string;
    decided_by_user_id: number;
}

interface AuditAnchor {
    id: string;
    action_type: string;
    actor_id: number | null;
    target_id: string | null;
    workspace_id: string | null;
    created_at: string;
}

interface Filters {
    decision_type?: string;
    workspace_id?: string;
}

interface PageProps {
    kpis: KPIs;
    by_decision_type: ByDecisionType[];
    by_human_decision: ByHumanDecision[];
    recent_decisions: DecisionRow[];
    recent_audit_anchors: AuditAnchor[];
    filters: Filters;
    valid_decision_types: string[];
}

function formatDate(iso: string | null): string {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleString();
    } catch {
        return iso;
    }
}

function shortUuid(uuid: string): string {
    return uuid.slice(0, 8) + '…';
}

function humanDecisionBadge(value: string): JSX.Element {
    const map: Record<string, string> = {
        accepted: 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300',
        signed_off: 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300',
        modified: 'border-amber-500/40 bg-amber-500/15 text-amber-300',
        rejected: 'border-red-500/40 bg-red-500/15 text-red-300',
    };
    const cls = map[value] ?? 'border-stone-700 bg-stone-800/40 text-stone-300';
    return (
        <span className={`rounded border px-2 py-0.5 text-xs ${cls}`}>{value}</span>
    );
}

export default function DecisionHistory({
    kpis,
    by_decision_type,
    by_human_decision,
    recent_decisions,
    recent_audit_anchors,
    filters,
    valid_decision_types,
}: PageProps): JSX.Element {
    // Phase 5 real-time push — RecordDecision::record (Laravel service)
    // dispatches `decision-history` after every successful decision commit.
    useAdminSurfaceUpdated('decision-history', null, () => {
        router.reload({ only: ['recent_decisions', 'recent_audit_anchors', 'kpis'] });
    });

    const setFilter = (key: string, value: string | null) => {
        const next: Record<string, string> = { ...filters } as never;
        if (value === null || value === '') {
            delete next[key];
        } else {
            next[key] = value;
        }
        router.get('/admin/decision-history', next, {
            preserveScroll: true,
            preserveState: true,
        });
    };

    return (
        <AppLayout>
            <Head title="Decision History — Admin" />
            <div className="min-h-screen bg-stone-950 text-stone-100">
                <div
                    className="mx-auto max-w-7xl px-6 py-8"
                    data-testid="decision-history"
                >
                    <Link
                        href="/dashboard"
                        className="mb-4 inline-block text-sm text-stone-400 hover:text-amber-300"
                    >
                        ← Back to dashboard
                    </Link>

                    <header className="mb-6 flex items-end justify-between gap-4">
                        <div>
                            <h1 className="text-2xl font-semibold text-stone-50">
                                Decision History
                            </h1>
                            <p className="mt-1 text-sm text-stone-400">
                                Cross-workspace view of §21 decision records + the audit
                                ledger entries they anchor. Master-plan §9.12.
                            </p>
                        </div>
                        <Link
                            href="/admin/eval-dashboard"
                            className="text-sm text-stone-400 hover:text-amber-300"
                        >
                            Eval Dashboard →
                        </Link>
                    </header>

                    {/* KPI tiles */}
                    <section className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
                        <Tile
                            label="Total decisions"
                            value={String(kpis.total_decisions)}
                            tone={kpis.total_decisions > 0 ? 'good' : 'neutral'}
                        />
                        <Tile
                            label="Audit-anchored"
                            value={`${kpis.decisions_with_audit_anchor} (${kpis.audit_anchor_pct}%)`}
                            tone={kpis.audit_anchor_pct === 100 ? 'good' : kpis.audit_anchor_pct >= 90 ? 'neutral' : 'bad'}
                        />
                        <Tile
                            label="Distinct workspaces"
                            value={String(kpis.distinct_workspaces)}
                        />
                        <Tile
                            label="Recent (30 d)"
                            value={String(kpis.recent_30d_count)}
                        />
                    </section>

                    {/* Filter strip */}
                    <section className="mb-6 flex flex-wrap items-center gap-2">
                        <span className="text-xs uppercase tracking-wide text-stone-500">
                            Filter:
                        </span>
                        <button
                            type="button"
                            onClick={() => setFilter('decision_type', null)}
                            className={`rounded border px-2 py-0.5 text-xs ${
                                !filters.decision_type
                                    ? 'border-amber-500/60 bg-amber-500/20 text-amber-200'
                                    : 'border-stone-700 bg-stone-800/40 text-stone-300 hover:border-stone-600'
                            }`}
                        >
                            all
                        </button>
                        {valid_decision_types.map((dt) => (
                            <button
                                key={dt}
                                type="button"
                                onClick={() => setFilter('decision_type', dt)}
                                className={`rounded border px-2 py-0.5 font-mono text-xs ${
                                    filters.decision_type === dt
                                        ? 'border-amber-500/60 bg-amber-500/20 text-amber-200'
                                        : 'border-stone-700 bg-stone-800/40 text-stone-300 hover:border-stone-600'
                                }`}
                            >
                                {dt}
                            </button>
                        ))}
                        {filters.workspace_id && (
                            <button
                                type="button"
                                onClick={() => setFilter('workspace_id', null)}
                                className="rounded border border-amber-500/60 bg-amber-500/20 px-2 py-0.5 font-mono text-xs text-amber-200 hover:border-amber-400"
                                title="Clear workspace filter"
                            >
                                workspace: {shortUuid(filters.workspace_id)} ✕
                            </button>
                        )}
                    </section>

                    {/* Per-decision-type breakdown */}
                    <section className="mb-6 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Per decision_type
                        </h2>
                        <div className="overflow-x-auto">
                            <table
                                className="w-full text-left text-sm"
                                data-testid="by-decision-type"
                            >
                                <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                    <tr>
                                        <th className="px-3 py-2">Type</th>
                                        <th className="px-3 py-2 text-right">Total</th>
                                        <th className="px-3 py-2 text-right">Accepted</th>
                                        <th className="px-3 py-2 text-right">Modified</th>
                                        <th className="px-3 py-2 text-right">Rejected</th>
                                        <th className="px-3 py-2 text-right">Signed off</th>
                                        <th className="px-3 py-2 text-right">Other</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {by_decision_type.length === 0 && (
                                        <tr>
                                            <td
                                                colSpan={7}
                                                className="px-3 py-8 text-center text-stone-500"
                                            >
                                                No decisions recorded yet. The §9.10{' '}
                                                <code className="text-stone-300">
                                                    record_decision
                                                </code>{' '}
                                                facade populates this surface when called
                                                from one of the 8 §21.3 capture hooks.
                                            </td>
                                        </tr>
                                    )}
                                    {by_decision_type.map((b) => (
                                        <tr
                                            key={b.decision_type}
                                            className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30"
                                        >
                                            <td className="px-3 py-2 font-mono text-xs text-stone-200">
                                                {b.decision_type}
                                            </td>
                                            <td className="px-3 py-2 text-right">
                                                {b.total}
                                            </td>
                                            <td className="px-3 py-2 text-right text-emerald-300">
                                                {b.accepted}
                                            </td>
                                            <td className="px-3 py-2 text-right text-amber-300">
                                                {b.modified}
                                            </td>
                                            <td className="px-3 py-2 text-right text-red-300">
                                                {b.rejected}
                                            </td>
                                            <td className="px-3 py-2 text-right text-emerald-300">
                                                {b.signed_off}
                                            </td>
                                            <td className="px-3 py-2 text-right text-stone-500">
                                                {b.other}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </section>

                    {/* Side-by-side: human decision rollup + mean uncertainty card */}
                    <section className="mb-6 grid grid-cols-1 gap-6 md:grid-cols-2">
                        <div className="rounded border border-stone-800 bg-stone-900">
                            <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                                By human_decision (cross-type)
                            </h2>
                            <ul className="divide-y divide-stone-800/60 text-sm">
                                {by_human_decision.length === 0 && (
                                    <li className="px-3 py-6 text-center text-stone-500">
                                        No decisions yet.
                                    </li>
                                )}
                                {by_human_decision.map((b) => (
                                    <li
                                        key={b.human_decision}
                                        className="flex items-center justify-between px-3 py-2"
                                    >
                                        {humanDecisionBadge(b.human_decision)}
                                        <span className="rounded bg-stone-800/60 px-2 py-0.5 text-xs text-stone-300">
                                            {b.count}
                                        </span>
                                    </li>
                                ))}
                            </ul>
                        </div>

                        <div className="rounded border border-stone-800 bg-stone-900 px-3 py-3 text-sm text-stone-300">
                            <h2 className="-mx-3 -mt-3 mb-3 border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                                Quality signals
                            </h2>
                            <div className="flex justify-between">
                                <span>Mean uncertainty</span>
                                <span className="font-mono">
                                    {kpis.mean_uncertainty !== null
                                        ? kpis.mean_uncertainty
                                        : '—'}
                                </span>
                            </div>
                            <div className="mt-1 flex justify-between">
                                <span>Audit-anchor coverage</span>
                                <span
                                    className={`font-mono ${
                                        kpis.audit_anchor_pct === 100
                                            ? 'text-emerald-300'
                                            : kpis.audit_anchor_pct >= 90
                                            ? 'text-amber-300'
                                            : 'text-red-300'
                                    }`}
                                >
                                    {kpis.audit_anchor_pct}%
                                </span>
                            </div>
                            <div className="mt-1 flex justify-between">
                                <span>Distinct deciders</span>
                                <span className="font-mono">
                                    {kpis.distinct_deciders}
                                </span>
                            </div>
                            <div className="mt-1 flex justify-between">
                                <span>Latest decision</span>
                                <span className="font-mono text-xs">
                                    {formatDate(kpis.latest_decided_at)}
                                </span>
                            </div>
                            <div className="mt-3 text-xs text-stone-500">
                                Per §29.2 export-compliance checklist, audit-anchor
                                coverage should be 100% for any decision flow gated by
                                §15.4 Export Compliance Agent.
                            </div>
                        </div>
                    </section>

                    {/* Recent decisions */}
                    <section className="mb-6 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Recent decisions (50)
                        </h2>
                        <div className="overflow-x-auto">
                            <table
                                className="w-full text-left text-sm"
                                data-testid="recent-decisions"
                            >
                                <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                    <tr>
                                        <th className="px-3 py-2">Decision</th>
                                        <th className="px-3 py-2">Workspace</th>
                                        <th className="px-3 py-2">Type</th>
                                        <th className="px-3 py-2">Recommendation</th>
                                        <th className="px-3 py-2">Decision</th>
                                        <th className="px-3 py-2 text-right">Uncert.</th>
                                        <th className="px-3 py-2">Anchor?</th>
                                        <th className="px-3 py-2">When</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {recent_decisions.length === 0 && (
                                        <tr>
                                            <td
                                                colSpan={8}
                                                className="px-3 py-8 text-center text-stone-500"
                                            >
                                                No decisions match the current filter.
                                            </td>
                                        </tr>
                                    )}
                                    {recent_decisions.map((d) => (
                                        <tr
                                            key={d.decision_id}
                                            className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30"
                                        >
                                            <td className="px-3 py-2 font-mono text-xs text-stone-300">
                                                {shortUuid(d.decision_id)}
                                            </td>
                                            <td className="px-3 py-2 font-mono text-xs text-stone-400">
                                                <button
                                                    type="button"
                                                    onClick={() => setFilter('workspace_id', d.workspace_id)}
                                                    className="hover:text-amber-300"
                                                    title="Filter to this workspace"
                                                >
                                                    {shortUuid(d.workspace_id)}
                                                </button>
                                            </td>
                                            <td className="px-3 py-2 font-mono text-xs text-stone-200">
                                                <button
                                                    type="button"
                                                    onClick={() => setFilter('decision_type', d.decision_type)}
                                                    className="hover:text-amber-300"
                                                    title="Filter to this type"
                                                >
                                                    {d.decision_type}
                                                </button>
                                            </td>
                                            <td className="max-w-md truncate px-3 py-2 text-xs text-stone-300">
                                                {d.recommendation}
                                            </td>
                                            <td className="px-3 py-2">
                                                {humanDecisionBadge(d.human_decision)}
                                            </td>
                                            <td className="px-3 py-2 text-right font-mono text-xs text-stone-400">
                                                {d.uncertainty !== null ? d.uncertainty : '—'}
                                            </td>
                                            <td className="px-3 py-2 text-xs">
                                                {d.has_audit_anchor ? (
                                                    <span className="text-emerald-400">✓</span>
                                                ) : (
                                                    <span className="text-red-400">✗</span>
                                                )}
                                            </td>
                                            <td className="px-3 py-2 text-xs text-stone-400">
                                                {formatDate(d.decided_at)}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </section>

                    {/* Recent audit anchors */}
                    <section className="mb-6 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Recent audit-ledger anchors (action_type LIKE 'decision.%')
                        </h2>
                        <div className="overflow-x-auto">
                            <table
                                className="w-full text-left text-sm"
                                data-testid="recent-audit-anchors"
                            >
                                <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                    <tr>
                                        <th className="px-3 py-2">Anchor</th>
                                        <th className="px-3 py-2">Action type</th>
                                        <th className="px-3 py-2">Actor</th>
                                        <th className="px-3 py-2">Decision target</th>
                                        <th className="px-3 py-2">Workspace</th>
                                        <th className="px-3 py-2">When</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {recent_audit_anchors.length === 0 && (
                                        <tr>
                                            <td
                                                colSpan={6}
                                                className="px-3 py-8 text-center text-stone-500"
                                            >
                                                No <code className="text-stone-300">
                                                    decision.*
                                                </code>{' '}
                                                audit ledger entries yet.
                                            </td>
                                        </tr>
                                    )}
                                    {recent_audit_anchors.map((a) => (
                                        <tr
                                            key={a.id}
                                            className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30"
                                        >
                                            <td className="px-3 py-2 font-mono text-xs text-stone-300">
                                                {shortUuid(a.id)}
                                            </td>
                                            <td className="px-3 py-2 font-mono text-xs text-emerald-300">
                                                {a.action_type}
                                            </td>
                                            <td className="px-3 py-2 text-xs text-stone-400">
                                                {a.actor_id ?? '—'}
                                            </td>
                                            <td className="px-3 py-2 font-mono text-xs text-stone-400">
                                                {a.target_id ? shortUuid(a.target_id) : '—'}
                                            </td>
                                            <td className="px-3 py-2 font-mono text-xs text-stone-400">
                                                {a.workspace_id ? shortUuid(a.workspace_id) : '—'}
                                            </td>
                                            <td className="px-3 py-2 text-xs text-stone-400">
                                                {formatDate(a.created_at)}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </section>

                    <footer className="mt-8 text-xs text-stone-500">
                        Read-only. Source tables:{' '}
                        <code className="text-stone-400">silver.decision_records</code>
                        ,{' '}
                        <code className="text-stone-400">audit.audit_ledger</code>.
                        Cross-workspace via the doc-phase 129 RLS admin escape hatch.
                    </footer>
                </div>
            </div>
        </AppLayout>
    );
}

function Tile({
    label,
    value,
    tone = 'neutral',
}: {
    label: string;
    value: string;
    tone?: 'good' | 'bad' | 'neutral';
}): JSX.Element {
    const tones: Record<string, string> = {
        good: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300',
        bad: 'border-red-500/40 bg-red-500/10 text-red-300',
        neutral: 'border-stone-800 bg-stone-900 text-stone-100',
    };
    return (
        <div className={`rounded border p-3 ${tones[tone] ?? tones.neutral}`}>
            <div className="text-xs uppercase tracking-wide opacity-80">{label}</div>
            <div className="mt-1 text-2xl font-semibold">{value}</div>
        </div>
    );
}
