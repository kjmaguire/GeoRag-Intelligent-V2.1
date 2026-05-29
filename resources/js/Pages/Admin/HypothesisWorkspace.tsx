import type { JSX } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

/**
 * /admin/hypothesis-workspace — Master-plan §9.10 Hypothesis Workspace
 * (doc-phase 131). Fourth Track-3 admin surface.
 *
 * Cross-workspace read-only view of silver.hypotheses + their evidence
 * links. Surfaces the §9.10 competing-hypotheses register from the
 * admin lens. Workspace-scoped writes happen via the §9.10 reasoning
 * agents (not yet graduated from skeleton).
 *
 * Backend: app/Http/Controllers/Admin/HypothesisWorkspaceController.php.
 */

interface KPIs {
    total_hypotheses: number;
    accepted_count: number;
    ai_suggested_count: number;
    mean_confidence: number | null;
    distinct_workspaces: number;
    distinct_parent_questions: number;
    total_evidence_links: number;
    recent_30d_count: number;
    latest_created_at: string | null;
}

interface CountByStatus {
    review_status: string;
    count: number;
}

interface CountByMethod {
    confidence_method: string;
    count: number;
}

interface CountByRole {
    role: string;
    count: number;
}

interface HypothesisRow {
    hypothesis_id: string;
    workspace_id: string;
    parent_question: string;
    label: string;
    description: string;
    confidence: number | null;
    confidence_method: string | null;
    review_status: string;
    reviewed_by_user_id: number | null;
    reviewed_at: string | null;
    created_at: string;
    supporting_count: number;
    contradicting_count: number;
    missing_count: number;
    recommended_test_count: number;
}

interface EvidenceLinkRow {
    link_id: string;
    hypothesis_id: string;
    hypothesis_label: string;
    workspace_id: string;
    source_chunk_id: string | null;
    role: string;
    weight: number | null;
}

interface Filters {
    review_status?: string;
    workspace_id?: string;
}

interface PageProps {
    kpis: KPIs;
    by_review_status: CountByStatus[];
    by_confidence_method: CountByMethod[];
    by_evidence_role: CountByRole[];
    recent_hypotheses: HypothesisRow[];
    recent_evidence_links: EvidenceLinkRow[];
    filters: Filters;
    valid_review_statuses: string[];
    valid_evidence_roles: string[];
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

function reviewStatusBadge(value: string): JSX.Element {
    const map: Record<string, string> = {
        ai_suggested: 'border-sky-500/40 bg-sky-500/15 text-sky-300',
        reviewed: 'border-amber-500/40 bg-amber-500/15 text-amber-300',
        accepted: 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300',
        rejected: 'border-red-500/40 bg-red-500/15 text-red-300',
    };
    const cls = map[value] ?? 'border-stone-700 bg-stone-800/40 text-stone-300';
    return (
        <span className={`rounded border px-2 py-0.5 text-xs ${cls}`}>{value}</span>
    );
}

function roleBadge(value: string): JSX.Element {
    const map: Record<string, string> = {
        supporting: 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300',
        contradicting: 'border-red-500/40 bg-red-500/15 text-red-300',
        missing: 'border-amber-500/40 bg-amber-500/15 text-amber-300',
        recommended_test: 'border-sky-500/40 bg-sky-500/15 text-sky-300',
    };
    const cls = map[value] ?? 'border-stone-700 bg-stone-800/40 text-stone-300';
    return (
        <span className={`rounded border px-2 py-0.5 text-xs ${cls}`}>{value}</span>
    );
}

export default function HypothesisWorkspace({
    kpis,
    by_review_status,
    by_confidence_method,
    by_evidence_role,
    recent_hypotheses,
    recent_evidence_links,
    filters,
    valid_review_statuses,
}: PageProps): JSX.Element {
    // Phase 5 real-time push — continuous_learning_loop +
    // field_outcome_learning both broadcast `hypothesis-workspace`.
    useAdminSurfaceUpdated('hypothesis-workspace', null, () => {
        router.reload({ only: ['recent_hypotheses', 'recent_evidence_links', 'kpis'] });
    });

    const setFilter = (key: string, value: string | null) => {
        const next: Record<string, string> = { ...filters } as never;
        if (value === null || value === '') {
            delete next[key];
        } else {
            next[key] = value;
        }
        router.get('/admin/hypothesis-workspace', next, {
            preserveScroll: true,
            preserveState: true,
        });
    };

    return (
        <AppLayout>
            <Head title="Hypothesis Workspace — Admin" />
            <div className="min-h-screen bg-stone-950 text-stone-100">
                <div
                    className="mx-auto max-w-7xl px-6 py-8"
                    data-testid="hypothesis-workspace"
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
                                Hypothesis Workspace
                            </h1>
                            <p className="mt-1 text-sm text-stone-400">
                                Cross-workspace view of competing hypotheses and their
                                evidence links. Master-plan §9.10.
                            </p>
                        </div>
                        <Link
                            href="/admin/decision-history"
                            className="text-sm text-stone-400 hover:text-amber-300"
                        >
                            Decision History →
                        </Link>
                    </header>

                    {/* KPI tiles */}
                    <section className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
                        <Tile
                            label="Total hypotheses"
                            value={String(kpis.total_hypotheses)}
                            tone={kpis.total_hypotheses > 0 ? 'good' : 'neutral'}
                        />
                        <Tile
                            label="Accepted"
                            value={String(kpis.accepted_count)}
                            tone={kpis.accepted_count > 0 ? 'good' : 'neutral'}
                        />
                        <Tile
                            label="Evidence links"
                            value={String(kpis.total_evidence_links)}
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
                            onClick={() => setFilter('review_status', null)}
                            className={`rounded border px-2 py-0.5 text-xs ${
                                !filters.review_status
                                    ? 'border-amber-500/60 bg-amber-500/20 text-amber-200'
                                    : 'border-stone-700 bg-stone-800/40 text-stone-300 hover:border-stone-600'
                            }`}
                        >
                            all
                        </button>
                        {valid_review_statuses.map((rs) => (
                            <button
                                key={rs}
                                type="button"
                                onClick={() => setFilter('review_status', rs)}
                                className={`rounded border px-2 py-0.5 font-mono text-xs ${
                                    filters.review_status === rs
                                        ? 'border-amber-500/60 bg-amber-500/20 text-amber-200'
                                        : 'border-stone-700 bg-stone-800/40 text-stone-300 hover:border-stone-600'
                                }`}
                            >
                                {rs}
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

                    {/* 3-up counts: review_status, confidence_method, evidence_role */}
                    <section className="mb-6 grid grid-cols-1 gap-6 md:grid-cols-3">
                        <CountPanel
                            title="By review_status"
                            rows={by_review_status.map((b) => ({
                                key: b.review_status,
                                badge: reviewStatusBadge(b.review_status),
                                count: b.count,
                            }))}
                            empty="No hypotheses yet."
                        />
                        <CountPanel
                            title="By confidence_method"
                            rows={by_confidence_method.map((b) => ({
                                key: b.confidence_method,
                                badge: (
                                    <span className="rounded border border-stone-700 bg-stone-800/40 px-2 py-0.5 font-mono text-xs text-stone-300">
                                        {b.confidence_method}
                                    </span>
                                ),
                                count: b.count,
                            }))}
                            empty="No hypotheses yet."
                        />
                        <CountPanel
                            title="By evidence role"
                            rows={by_evidence_role.map((b) => ({
                                key: b.role,
                                badge: roleBadge(b.role),
                                count: b.count,
                            }))}
                            empty="No evidence links yet."
                        />
                    </section>

                    {/* Quality signals card */}
                    <section className="mb-6 rounded border border-stone-800 bg-stone-900 px-4 py-3 text-sm text-stone-300">
                        <h2 className="-mx-4 -mt-3 mb-3 border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Quality signals
                        </h2>
                        <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
                            <div className="flex justify-between">
                                <span>Mean confidence</span>
                                <span className="font-mono">
                                    {kpis.mean_confidence !== null
                                        ? kpis.mean_confidence
                                        : '—'}
                                </span>
                            </div>
                            <div className="flex justify-between">
                                <span>Distinct workspaces</span>
                                <span className="font-mono">
                                    {kpis.distinct_workspaces}
                                </span>
                            </div>
                            <div className="flex justify-between">
                                <span>Distinct parent questions</span>
                                <span className="font-mono">
                                    {kpis.distinct_parent_questions}
                                </span>
                            </div>
                        </div>
                    </section>

                    {/* Recent hypotheses table */}
                    <section className="mb-6 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Recent hypotheses (last 50)
                        </h2>
                        <div className="overflow-x-auto">
                            <table
                                className="w-full text-left text-sm"
                                data-testid="recent-hypotheses"
                            >
                                <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                    <tr>
                                        <th className="px-3 py-2">ID</th>
                                        <th className="px-3 py-2">Label</th>
                                        <th className="px-3 py-2">Parent question</th>
                                        <th className="px-3 py-2">Description</th>
                                        <th className="px-3 py-2">Status</th>
                                        <th className="px-3 py-2 text-right">Conf.</th>
                                        <th className="px-3 py-2 text-right" title="supporting / contradicting / missing / recommended_test">
                                            S/C/M/T
                                        </th>
                                        <th className="px-3 py-2">Workspace</th>
                                        <th className="px-3 py-2">Created</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {recent_hypotheses.length === 0 && (
                                        <tr>
                                            <td
                                                colSpan={9}
                                                className="px-3 py-8 text-center text-stone-500"
                                            >
                                                No hypotheses recorded yet. The §9.10
                                                competing-hypothesis register populates this
                                                surface once the reasoning agents that emit
                                                ai_suggested hypotheses graduate from skeleton.
                                            </td>
                                        </tr>
                                    )}
                                    {recent_hypotheses.map((h) => (
                                        <tr
                                            key={h.hypothesis_id}
                                            className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30"
                                        >
                                            <td className="px-3 py-2 font-mono text-xs text-stone-300">
                                                {shortUuid(h.hypothesis_id)}
                                            </td>
                                            <td className="px-3 py-2 font-mono text-xs text-amber-300">
                                                {h.label}
                                            </td>
                                            <td
                                                className="max-w-xs truncate px-3 py-2 text-xs text-stone-300"
                                                title={h.parent_question}
                                            >
                                                {h.parent_question}
                                            </td>
                                            <td
                                                className="max-w-xs truncate px-3 py-2 text-xs text-stone-400"
                                                title={h.description}
                                            >
                                                {h.description}
                                            </td>
                                            <td className="px-3 py-2">
                                                {reviewStatusBadge(h.review_status)}
                                            </td>
                                            <td className="px-3 py-2 text-right font-mono text-xs text-stone-300">
                                                {h.confidence !== null ? h.confidence : '—'}
                                            </td>
                                            <td className="px-3 py-2 text-right font-mono text-xs">
                                                <span className="text-emerald-300">
                                                    {h.supporting_count}
                                                </span>
                                                <span className="text-stone-600">/</span>
                                                <span className="text-red-300">
                                                    {h.contradicting_count}
                                                </span>
                                                <span className="text-stone-600">/</span>
                                                <span className="text-amber-300">
                                                    {h.missing_count}
                                                </span>
                                                <span className="text-stone-600">/</span>
                                                <span className="text-sky-300">
                                                    {h.recommended_test_count}
                                                </span>
                                            </td>
                                            <td className="px-3 py-2 font-mono text-xs text-stone-400">
                                                <button
                                                    type="button"
                                                    onClick={() =>
                                                        setFilter('workspace_id', h.workspace_id)
                                                    }
                                                    className="hover:text-amber-300"
                                                    title={h.workspace_id}
                                                >
                                                    {shortUuid(h.workspace_id)}
                                                </button>
                                            </td>
                                            <td className="px-3 py-2 text-xs text-stone-400">
                                                {formatDate(h.created_at)}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </section>

                    {/* Recent evidence links */}
                    <section className="mb-6 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Recent evidence links (last 100)
                        </h2>
                        <div className="overflow-x-auto">
                            <table
                                className="w-full text-left text-sm"
                                data-testid="recent-evidence-links"
                            >
                                <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                    <tr>
                                        <th className="px-3 py-2">Link</th>
                                        <th className="px-3 py-2">Hypothesis</th>
                                        <th className="px-3 py-2">Role</th>
                                        <th className="px-3 py-2 text-right">Weight</th>
                                        <th className="px-3 py-2">Source chunk</th>
                                        <th className="px-3 py-2">Workspace</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {recent_evidence_links.length === 0 && (
                                        <tr>
                                            <td
                                                colSpan={6}
                                                className="px-3 py-8 text-center text-stone-500"
                                            >
                                                No evidence links yet.
                                            </td>
                                        </tr>
                                    )}
                                    {recent_evidence_links.map((l) => (
                                        <tr
                                            key={l.link_id}
                                            className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30"
                                        >
                                            <td className="px-3 py-2 font-mono text-xs text-stone-300">
                                                {shortUuid(l.link_id)}
                                            </td>
                                            <td className="px-3 py-2 font-mono text-xs">
                                                <span className="text-amber-300">
                                                    {l.hypothesis_label}
                                                </span>
                                                <span className="ml-2 text-stone-500">
                                                    {shortUuid(l.hypothesis_id)}
                                                </span>
                                            </td>
                                            <td className="px-3 py-2">{roleBadge(l.role)}</td>
                                            <td className="px-3 py-2 text-right font-mono text-xs text-stone-300">
                                                {l.weight !== null ? l.weight : '—'}
                                            </td>
                                            <td className="px-3 py-2 font-mono text-xs text-stone-400">
                                                {l.source_chunk_id ?? '—'}
                                            </td>
                                            <td className="px-3 py-2 font-mono text-xs text-stone-400">
                                                {shortUuid(l.workspace_id)}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </section>

                    <footer className="mt-8 text-xs text-stone-500">
                        Read-only. Source tables:{' '}
                        <code className="text-stone-400">silver.hypotheses</code>,{' '}
                        <code className="text-stone-400">
                            silver.hypothesis_evidence_links
                        </code>
                        . Cross-workspace via the doc-phase 129 RLS admin escape hatch
                        (inherited transitively on the evidence_links policy).
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

function CountPanel({
    title,
    rows,
    empty,
}: {
    title: string;
    rows: { key: string; badge: JSX.Element; count: number }[];
    empty: string;
}): JSX.Element {
    return (
        <div className="rounded border border-stone-800 bg-stone-900">
            <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                {title}
            </h2>
            <ul className="divide-y divide-stone-800/60 text-sm">
                {rows.length === 0 && (
                    <li className="px-3 py-6 text-center text-stone-500">{empty}</li>
                )}
                {rows.map((r) => (
                    <li
                        key={r.key}
                        className="flex items-center justify-between px-3 py-2"
                    >
                        {r.badge}
                        <span className="rounded bg-stone-800/60 px-2 py-0.5 text-xs text-stone-300">
                            {r.count}
                        </span>
                    </li>
                ))}
            </ul>
        </div>
    );
}
