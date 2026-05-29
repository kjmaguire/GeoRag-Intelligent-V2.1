import type { JSX } from 'react';
import { useState } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

// Phase G.5 follow-up — Support Cockpit can now invoke the 5 phase10
// support agents via /admin/support-cockpit/agents/{agent}.
type AgentName =
    | 'ticket-triage'
    | 'support-packet'
    | 'root-cause-investigation'
    | 'customer-response-draft'
    | 'escalation-routing';

const AGENT_LABELS: Record<AgentName, string> = {
    'ticket-triage': 'Triage',
    'support-packet': 'Packet',
    'root-cause-investigation': 'Root cause',
    'customer-response-draft': 'Draft reply',
    'escalation-routing': 'Routing',
};

/**
 * /admin/support-cockpit — Master-plan §10.11 / §25 Customer Support
 * Cockpit (doc-phase 130).
 *
 * Cross-workspace read-only view of ops.support_tickets + the audit-ledger
 * trail every cross-workspace access leaves behind (§25.3). Backend:
 * app/Http/Controllers/Admin/SupportCockpitController.php.
 */

interface KPIs {
    total_tickets: number;
    open_tickets: number;
    critical_open: number;
    unassigned_open: number;
    resolved_30d: number;
    mean_resolution_hours: number | null;
    total_support_accesses_30d: number;
    latest_ticket_at: string | null;
}

interface CountByStatus {
    status: string;
    count: number;
}

interface CountBySeverity {
    severity: string;
    count: number;
}

interface CountByCategory {
    category: string;
    count: number;
}

interface TicketRow {
    ticket_id: string;
    workspace_id: string | null;
    reported_by_user_id: number | null;
    reported_at: string;
    channel: string;
    category: string;
    description: string;
    severity: string;
    assigned_to_user_id: number | null;
    status: string;
    resolved_at: string | null;
    age_hours: number;
}

interface AccessRow {
    id: string;
    created_at: string;
    actor_id: number | null;
    workspace_id: string | null;
    target_id: string | null;
    access_kind: string | null;
    target_summary: string | null;
}

interface ReplayRow {
    replay_id: string;
    ticket_id: string;
    original_workflow_run_id: string;
    dry_run: boolean;
    initiated_by_user_id: number;
    initiated_at: string;
    status: string;
}

interface Filters {
    status?: string;
    severity?: string;
    category?: string;
}

interface PageProps {
    kpis: KPIs;
    by_status: CountByStatus[];
    by_severity: CountBySeverity[];
    by_category: CountByCategory[];
    recent_tickets: TicketRow[];
    recent_accesses: AccessRow[];
    recent_replays: ReplayRow[];
    /** §10.13 — LangFuse base URL for trace deep-links; empty when
     * LANGFUSE_BASE_URL env var isn't set (page falls back to copyable
     * trace IDs). */
    langfuse_base_url?: string;
    filters: Filters;
    valid_statuses: string[];
    valid_severities: string[];
    valid_categories: string[];
}

function shortUuid(uuid: string | null): string {
    if (!uuid) return '—';
    return uuid.slice(0, 8) + '…';
}

function formatDate(iso: string | null): string {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleString();
    } catch {
        return iso;
    }
}

function formatAge(hours: number): string {
    if (hours < 1) return `${Math.round(hours * 60)}m`;
    if (hours < 24) return `${hours.toFixed(1)}h`;
    return `${Math.round(hours / 24)}d`;
}

function statusBadge(s: string): JSX.Element {
    const map: Record<string, string> = {
        open: 'border-red-500/40 bg-red-500/15 text-red-300',
        investigating: 'border-amber-500/40 bg-amber-500/15 text-amber-300',
        resolved: 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300',
        closed: 'border-stone-700 bg-stone-800/40 text-stone-400',
    };
    const cls = map[s] ?? 'border-stone-700 bg-stone-800/40 text-stone-300';
    return <span className={`rounded border px-2 py-0.5 text-xs ${cls}`}>{s}</span>;
}

function severityBadge(s: string): JSX.Element {
    const map: Record<string, string> = {
        critical: 'border-red-500/60 bg-red-500/20 text-red-200 font-semibold',
        high: 'border-red-500/40 bg-red-500/10 text-red-300',
        medium: 'border-amber-500/40 bg-amber-500/15 text-amber-300',
        low: 'border-sky-500/40 bg-sky-500/15 text-sky-300',
    };
    const cls = map[s] ?? 'border-stone-700 bg-stone-800/40 text-stone-300';
    return <span className={`rounded border px-2 py-0.5 text-xs ${cls}`}>{s}</span>;
}

export default function SupportCockpit({
    kpis,
    by_status,
    by_severity,
    by_category,
    recent_tickets,
    recent_accesses,
    recent_replays,
    filters,
    valid_statuses,
    valid_severities,
    valid_categories,
    langfuse_base_url,
}: PageProps): JSX.Element {
    // Phase 5 real-time push — support_replay (Phase 2 wired) broadcasts
    // `support-cockpit`. Phase 2 missed wiring this Admin page; only the
    // Foundry sibling page got the hook. Closes the gap.
    useAdminSurfaceUpdated('support-cockpit', null, () => {
        router.reload({ only: ['recent_tickets', 'recent_replays', 'recent_accesses', 'kpis'] });
    });

    // §10.13 — render a workflow_run_id either as a copyable mono span
    // (when LangFuse base URL is unset) or as an external link into the
    // trace viewer. LangFuse trace IDs follow the `/trace/<id>` pattern
    // by convention; the GeoRAG support cockpit uses workflow_run_id as
    // the trace_id (set on the workflow's Context).
    const renderTraceLink = (runId: string): JSX.Element => {
        const label = `${runId.slice(0, 12)}…`;
        if (!langfuse_base_url) {
            return (
                <span
                    className="cursor-pointer hover:text-stone-100"
                    title={`Click to copy: ${runId}`}
                    onClick={() => navigator.clipboard?.writeText(runId)}
                >
                    {label}
                </span>
            );
        }
        return (
            <a
                href={`${langfuse_base_url}/trace/${encodeURIComponent(runId)}`}
                target="_blank"
                rel="noopener noreferrer"
                className="text-sky-300 underline-offset-2 hover:underline"
                title={`Open trace in LangFuse: ${runId}`}
            >
                {label} ↗
            </a>
        );
    };
    const setFilter = (key: keyof Filters, value: string | null) => {
        const next: Record<string, string> = { ...filters } as never;
        if (value === null || value === '') {
            delete next[key];
        } else {
            next[key] = value;
        }
        router.get('/admin/support-cockpit', next, {
            preserveScroll: true,
            preserveState: true,
        });
    };

    // Phase G.5 follow-up — agent invocation state.
    const [agentResult, setAgentResult] = useState<{
        agent: AgentName;
        ticket_id: string;
        loading: boolean;
        payload?: unknown;
        error?: string;
    } | null>(null);

    const runAgent = async (agent: AgentName, ticketId: string): Promise<void> => {
        // Each agent has its own body requirements. We supply ticket_id
        // for all of them; customer-response-draft needs a
        // resolution_summary which we prompt for inline.
        const body: Record<string, unknown> = { ticket_id: ticketId };
        if (agent === 'customer-response-draft') {
            const summary = window.prompt(
                'Resolution summary (1-2 sentences describing the fix):',
                '',
            );
            if (!summary || !summary.trim()) {
                return;
            }
            body.resolution_summary = summary.trim();
        }

        setAgentResult({ agent, ticket_id: ticketId, loading: true });
        try {
            const csrf = document
                .querySelector('meta[name="csrf-token"]')
                ?.getAttribute('content') ?? '';
            const resp = await fetch(
                `/admin/support-cockpit/agents/${agent}`,
                {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'application/json',
                        'X-CSRF-TOKEN': csrf,
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                    body: JSON.stringify(body),
                },
            );
            const payload = await resp.json();
            if (!resp.ok) {
                setAgentResult({
                    agent,
                    ticket_id: ticketId,
                    loading: false,
                    error: `HTTP ${resp.status}: ${JSON.stringify(payload)}`,
                });
                return;
            }
            setAgentResult({
                agent,
                ticket_id: ticketId,
                loading: false,
                payload,
            });
        } catch (exc) {
            setAgentResult({
                agent,
                ticket_id: ticketId,
                loading: false,
                error: String(exc),
            });
        }
    };

    return (
        <AppLayout>
            <Head title="Support Cockpit — Admin" />
            <div className="min-h-screen bg-stone-950 text-stone-100">
                <div
                    className="mx-auto max-w-7xl px-6 py-8"
                    data-testid="support-cockpit"
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
                                Customer Support Cockpit
                            </h1>
                            <p className="mt-1 text-sm text-stone-400">
                                Cross-workspace ticket inventory + the audit-ledger trail
                                of every ops access. Master-plan §25.
                            </p>
                        </div>
                        <div className="flex gap-3 text-sm text-stone-400">
                            <Link
                                href="/admin/eval-dashboard"
                                className="hover:text-amber-300"
                            >
                                Eval Dashboard
                            </Link>
                            <Link
                                href="/admin/decision-history"
                                className="hover:text-amber-300"
                            >
                                Decision History →
                            </Link>
                        </div>
                    </header>

                    {/* KPI tiles */}
                    <section className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
                        <Tile
                            label="Open tickets"
                            value={String(kpis.open_tickets)}
                            tone={kpis.open_tickets > 0 ? 'bad' : 'neutral'}
                        />
                        <Tile
                            label="Critical open"
                            value={String(kpis.critical_open)}
                            tone={kpis.critical_open > 0 ? 'bad' : 'neutral'}
                        />
                        <Tile
                            label="Unassigned open"
                            value={String(kpis.unassigned_open)}
                            tone={kpis.unassigned_open > 0 ? 'bad' : 'neutral'}
                        />
                        <Tile
                            label="Resolved (30 d)"
                            value={String(kpis.resolved_30d)}
                            tone={kpis.resolved_30d > 0 ? 'good' : 'neutral'}
                        />
                    </section>

                    {/* Filter strip */}
                    <section className="mb-6 space-y-2">
                        <FilterRow
                            label="Status"
                            active={filters.status}
                            values={valid_statuses}
                            onPick={(v) => setFilter('status', v)}
                        />
                        <FilterRow
                            label="Severity"
                            active={filters.severity}
                            values={valid_severities}
                            onPick={(v) => setFilter('severity', v)}
                        />
                        <FilterRow
                            label="Category"
                            active={filters.category}
                            values={valid_categories}
                            onPick={(v) => setFilter('category', v)}
                        />
                    </section>

                    {/* Three count panels side-by-side */}
                    <section className="mb-6 grid grid-cols-1 gap-6 md:grid-cols-3">
                        <CountPanel
                            title="By status"
                            rows={by_status.map((r) => ({
                                label: r.status,
                                count: r.count,
                                badge: statusBadge(r.status),
                            }))}
                            emptyHint="No tickets recorded yet."
                        />
                        <CountPanel
                            title="By severity (active)"
                            rows={by_severity.map((r) => ({
                                label: r.severity,
                                count: r.count,
                                badge: severityBadge(r.severity),
                            }))}
                            emptyHint="No active tickets."
                        />
                        <CountPanel
                            title="By category"
                            rows={by_category.map((r) => ({
                                label: r.category,
                                count: r.count,
                            }))}
                            emptyHint="No tickets."
                        />
                    </section>

                    {/* Recent tickets table */}
                    <section className="mb-6 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Recent tickets (50)
                        </h2>
                        <div className="overflow-x-auto">
                            <table
                                className="w-full text-left text-sm"
                                data-testid="recent-tickets"
                            >
                                <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                    <tr>
                                        <th className="px-3 py-2">Ticket</th>
                                        <th className="px-3 py-2">Workspace</th>
                                        <th className="px-3 py-2">Category</th>
                                        <th className="px-3 py-2">Description</th>
                                        <th className="px-3 py-2">Severity</th>
                                        <th className="px-3 py-2">Status</th>
                                        <th className="px-3 py-2 text-right">Age</th>
                                        <th className="px-3 py-2">Reported</th>
                                        <th className="px-3 py-2">Agents</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {recent_tickets.length === 0 && (
                                        <tr>
                                            <td
                                                colSpan={9}
                                                className="px-3 py-8 text-center text-stone-500"
                                            >
                                                No tickets match the current filter.
                                                Tickets land here when customers report
                                                issues via the in-app form, email,
                                                webhook, or phone channel.
                                            </td>
                                        </tr>
                                    )}
                                    {recent_tickets.map((t) => (
                                        <tr
                                            key={t.ticket_id}
                                            className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30"
                                        >
                                            <td className="px-3 py-2 font-mono text-xs text-stone-300">
                                                {shortUuid(t.ticket_id)}
                                            </td>
                                            <td className="px-3 py-2 font-mono text-xs text-stone-400">
                                                {shortUuid(t.workspace_id)}
                                            </td>
                                            <td className="px-3 py-2 font-mono text-xs">
                                                <button
                                                    type="button"
                                                    onClick={() => setFilter('category', t.category)}
                                                    className="text-stone-200 hover:text-amber-300"
                                                >
                                                    {t.category}
                                                </button>
                                            </td>
                                            <td className="max-w-md truncate px-3 py-2 text-xs text-stone-300">
                                                {t.description}
                                            </td>
                                            <td className="px-3 py-2">
                                                <button
                                                    type="button"
                                                    onClick={() => setFilter('severity', t.severity)}
                                                >
                                                    {severityBadge(t.severity)}
                                                </button>
                                            </td>
                                            <td className="px-3 py-2">
                                                <button
                                                    type="button"
                                                    onClick={() => setFilter('status', t.status)}
                                                >
                                                    {statusBadge(t.status)}
                                                </button>
                                            </td>
                                            <td className="px-3 py-2 text-right font-mono text-xs text-stone-400">
                                                {formatAge(t.age_hours)}
                                            </td>
                                            <td className="px-3 py-2 text-xs text-stone-400">
                                                {formatDate(t.reported_at)}
                                            </td>
                                            <td className="px-3 py-2">
                                                <div className="flex flex-wrap gap-1">
                                                    {(Object.keys(AGENT_LABELS) as AgentName[]).map((agent) => (
                                                        <button
                                                            key={agent}
                                                            type="button"
                                                            onClick={() => runAgent(agent, t.ticket_id)}
                                                            disabled={agentResult?.loading && agentResult.ticket_id === t.ticket_id}
                                                            className="rounded border border-stone-700 bg-stone-800 px-2 py-0.5 text-[10px] text-stone-300 hover:border-amber-500 hover:text-amber-300 disabled:cursor-wait disabled:opacity-50"
                                                            title={`Run ${agent} on this ticket`}
                                                        >
                                                            {AGENT_LABELS[agent]}
                                                        </button>
                                                    ))}
                                                </div>
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </section>

                    {/* Recent support access audits */}
                    <section className="mb-6 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Recent support access audit anchors (100) — §25.3 forensic
                            trail
                        </h2>
                        <div className="overflow-x-auto">
                            <table
                                className="w-full text-left text-sm"
                                data-testid="recent-accesses"
                            >
                                <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                    <tr>
                                        <th className="px-3 py-2">Anchor</th>
                                        <th className="px-3 py-2">Actor</th>
                                        <th className="px-3 py-2">Workspace</th>
                                        <th className="px-3 py-2">Access kind</th>
                                        <th className="px-3 py-2">Target summary</th>
                                        <th className="px-3 py-2">When</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {recent_accesses.length === 0 && (
                                        <tr>
                                            <td
                                                colSpan={6}
                                                className="px-3 py-8 text-center text-stone-500"
                                            >
                                                No <code className="text-stone-300">
                                                    support_access
                                                </code>{' '}
                                                audit entries yet.
                                            </td>
                                        </tr>
                                    )}
                                    {recent_accesses.map((a) => (
                                        <tr
                                            key={a.id}
                                            className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30"
                                        >
                                            <td className="px-3 py-2 font-mono text-xs text-stone-300">
                                                {shortUuid(a.id)}
                                            </td>
                                            <td className="px-3 py-2 text-xs text-stone-400">
                                                {a.actor_id ?? '—'}
                                            </td>
                                            <td className="px-3 py-2 font-mono text-xs text-stone-400">
                                                {shortUuid(a.workspace_id)}
                                            </td>
                                            <td className="px-3 py-2 font-mono text-xs text-emerald-300">
                                                {a.access_kind ?? '—'}
                                            </td>
                                            <td className="max-w-md truncate px-3 py-2 text-xs text-stone-300">
                                                {a.target_summary ?? '—'}
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

                    {/* Recent replay runs */}
                    <section className="mb-6 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Recent replay runs (30) — §10.10 support_replay
                        </h2>
                        <div className="overflow-x-auto">
                            <table
                                className="w-full text-left text-sm"
                                data-testid="recent-replays"
                            >
                                <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                    <tr>
                                        <th className="px-3 py-2">Replay</th>
                                        <th className="px-3 py-2">Ticket</th>
                                        <th className="px-3 py-2">Original run</th>
                                        <th className="px-3 py-2">Dry-run?</th>
                                        <th className="px-3 py-2">Initiated by</th>
                                        <th className="px-3 py-2">Status</th>
                                        <th className="px-3 py-2">When</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {recent_replays.length === 0 && (
                                        <tr>
                                            <td
                                                colSpan={7}
                                                className="px-3 py-8 text-center text-stone-500"
                                            >
                                                No replay runs yet. The §10.10{' '}
                                                <code className="text-stone-300">
                                                    support_replay
                                                </code>{' '}
                                                Hatchet workflow populates this surface
                                                once its task body graduates.
                                            </td>
                                        </tr>
                                    )}
                                    {recent_replays.map((r) => (
                                        <tr
                                            key={r.replay_id}
                                            className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30"
                                        >
                                            <td className="px-3 py-2 font-mono text-xs text-stone-300">
                                                {shortUuid(r.replay_id)}
                                            </td>
                                            <td className="px-3 py-2 font-mono text-xs text-stone-400">
                                                {shortUuid(r.ticket_id)}
                                            </td>
                                            <td className="px-3 py-2 font-mono text-xs text-stone-400">
                                                {renderTraceLink(r.original_workflow_run_id)}
                                            </td>
                                            <td className="px-3 py-2 text-xs">
                                                {r.dry_run ? (
                                                    <span className="rounded border border-sky-500/40 bg-sky-500/15 px-2 py-0.5 text-sky-300">
                                                        dry-run
                                                    </span>
                                                ) : (
                                                    <span className="rounded border border-amber-500/40 bg-amber-500/15 px-2 py-0.5 text-amber-300">
                                                        LIVE
                                                    </span>
                                                )}
                                            </td>
                                            <td className="px-3 py-2 text-xs text-stone-400">
                                                {r.initiated_by_user_id}
                                            </td>
                                            <td className="px-3 py-2 text-xs">
                                                {r.status}
                                            </td>
                                            <td className="px-3 py-2 text-xs text-stone-400">
                                                {formatDate(r.initiated_at)}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </section>

                    <footer className="mt-8 text-xs text-stone-500">
                        Read-only. Source tables:{' '}
                        <code className="text-stone-400">ops.support_tickets</code>,{' '}
                        <code className="text-stone-400">ops.support_replay_runs</code>,{' '}
                        <code className="text-stone-400">audit.audit_ledger</code>{' '}
                        (action_type='support_access'). §25.3 cross-workspace access
                        audit guarantees apply.
                    </footer>
                </div>
            </div>
            {/* Phase G.5 follow-up — agent result modal */}
            {agentResult && (
                <div
                    className="fixed inset-0 z-50 flex items-center justify-center bg-stone-950/80 p-4"
                    onClick={() => setAgentResult(null)}
                >
                    <div
                        className="max-h-[80vh] w-full max-w-3xl overflow-auto rounded border border-stone-700 bg-stone-900 p-6 shadow-xl"
                        onClick={(e) => e.stopPropagation()}
                    >
                        <header className="mb-4 flex items-start justify-between gap-4">
                            <div>
                                <h2 className="text-lg font-semibold text-amber-300">
                                    {AGENT_LABELS[agentResult.agent]} — agent output
                                </h2>
                                <p className="mt-1 font-mono text-xs text-stone-400">
                                    ticket {shortUuid(agentResult.ticket_id)}
                                </p>
                            </div>
                            <button
                                type="button"
                                onClick={() => setAgentResult(null)}
                                className="rounded border border-stone-700 px-2 py-1 text-xs text-stone-400 hover:border-stone-500"
                            >
                                Close
                            </button>
                        </header>
                        {agentResult.loading && (
                            <p className="text-stone-400">Running agent…</p>
                        )}
                        {agentResult.error && (
                            <pre className="overflow-auto rounded bg-red-950/60 p-3 text-xs text-red-200">
                                {agentResult.error}
                            </pre>
                        )}
                        {agentResult.payload !== undefined && !agentResult.error && (
                            <pre className="max-h-[60vh] overflow-auto rounded bg-stone-950 p-3 text-xs text-stone-200">
                                {JSON.stringify(agentResult.payload, null, 2)}
                            </pre>
                        )}
                    </div>
                </div>
            )}
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

function FilterRow({
    label,
    active,
    values,
    onPick,
}: {
    label: string;
    active: string | undefined;
    values: string[];
    onPick: (v: string | null) => void;
}): JSX.Element {
    return (
        <div className="flex flex-wrap items-center gap-2">
            <span className="w-20 text-xs uppercase tracking-wide text-stone-500">
                {label}:
            </span>
            <button
                type="button"
                onClick={() => onPick(null)}
                className={`rounded border px-2 py-0.5 text-xs ${
                    !active
                        ? 'border-amber-500/60 bg-amber-500/20 text-amber-200'
                        : 'border-stone-700 bg-stone-800/40 text-stone-300 hover:border-stone-600'
                }`}
            >
                all
            </button>
            {values.map((v) => (
                <button
                    key={v}
                    type="button"
                    onClick={() => onPick(v)}
                    className={`rounded border px-2 py-0.5 font-mono text-xs ${
                        active === v
                            ? 'border-amber-500/60 bg-amber-500/20 text-amber-200'
                            : 'border-stone-700 bg-stone-800/40 text-stone-300 hover:border-stone-600'
                    }`}
                >
                    {v}
                </button>
            ))}
        </div>
    );
}

function CountPanel({
    title,
    rows,
    emptyHint,
}: {
    title: string;
    rows: Array<{ label: string; count: number; badge?: JSX.Element }>;
    emptyHint: string;
}): JSX.Element {
    return (
        <div className="rounded border border-stone-800 bg-stone-900">
            <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                {title}
            </h2>
            <ul className="divide-y divide-stone-800/60 text-sm">
                {rows.length === 0 && (
                    <li className="px-3 py-6 text-center text-stone-500">{emptyHint}</li>
                )}
                {rows.map((r) => (
                    <li
                        key={r.label}
                        className="flex items-center justify-between px-3 py-2"
                    >
                        {r.badge ?? (
                            <span className="font-mono text-xs text-stone-200">
                                {r.label}
                            </span>
                        )}
                        <span className="rounded bg-stone-800/60 px-2 py-0.5 text-xs text-stone-300">
                            {r.count}
                        </span>
                    </li>
                ))}
            </ul>
        </div>
    );
}
