import type { JSX } from 'react';
import { useState } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

/**
 * /admin/alerts-inbox — audit-anchored alerts surface (Phase H4).
 *
 * Lists rows from audit.audit_ledger where action_type LIKE '%.alert'
 * (cost.burn.alert, vllm_security.alert, etc.) with an Acknowledge
 * action that writes a `<action_type>.acknowledged` counter row.
 * Both alert and ack are immutable — the timeline is reconstructable.
 */

type Alert = {
    audit_id: string;
    action_type: string;
    workspace_id: string | null;
    actor_id: number | null;
    actor_kind: string | null;
    target_schema: string | null;
    target_table: string | null;
    target_id: string | null;
    severity: string | null;
    payload: Record<string, unknown>;
    created_at: string;
    acknowledged_at: string | null;
    acknowledged_by_user_id: number | null;
};

type PageProps = {
    items: Alert[];
    total: number;
    page: number;
    per_page: number;
    fastapi_error: string | null;
    include_acknowledged: boolean;
    filter_workspace_id: string | null;
    filter_action_type_prefix: string | null;
};

function severityBadge(action_type: string, severity: string | null): JSX.Element {
    const sev = severity ?? (action_type.includes('vllm_security') ? 'high' : 'medium');
    const cls =
        sev === 'critical' ? 'bg-red-200 text-red-900 border-red-300' :
        sev === 'high'     ? 'bg-amber-200 text-amber-900 border-amber-300' :
        sev === 'low'      ? 'bg-slate-200 text-slate-800 border-slate-300' :
                             'bg-yellow-100 text-yellow-900 border-yellow-300';
    return <span className={`px-1.5 py-0.5 text-[10px] uppercase font-semibold rounded border ${cls}`}>{sev}</span>;
}

type Severity = 'all' | 'critical' | 'high' | 'medium' | 'low';

const SEVERITY_RANK: Record<string, number> = {
    critical: 4, high: 3, medium: 2, low: 1, '': 0,
};

function inferSeverity(item: Alert): string {
    if (item.severity) return item.severity;
    if (item.action_type.includes('vllm_security')) return 'high';
    if (item.action_type.includes('cost.burn')) return 'medium';
    return 'medium';
}

export default function AlertsInbox({
    items, total, page, per_page,
    fastapi_error, include_acknowledged,
    filter_workspace_id, filter_action_type_prefix,
}: PageProps): JSX.Element {
    // Phase 2 real-time push — the AuditEmitter (Laravel) + emit_audit
    // (FastAPI) helpers broadcast to admin.alerts-inbox whenever an
    // audit row with action_type ending in '.alert' or '.acknowledged'
    // is committed. One hook covers every alert writer (cost_burn_watcher,
    // reliability_metrics_publisher, stale_run_detector, vllm_security,
    // any future).
    useAdminSurfaceUpdated('alerts-inbox', null, () => {
        router.reload({ only: ['items'] });
    });

    const [expanded, setExpanded] = useState<string | null>(null);
    const [acking, setAcking] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [severityFilter, setSeverityFilter] = useState<Severity>('all');
    const [actionTypeFilter, setActionTypeFilter] = useState<string>(filter_action_type_prefix ?? '');
    const [sortBy, setSortBy] = useState<'created_at' | 'severity'>('created_at');
    const [verifyState, setVerifyState] = useState<{ ok: boolean; rows: number; reason: string | null } | null>(null);
    const [verifying, setVerifying] = useState<boolean>(false);

    async function verifyChain(): Promise<void> {
        setVerifying(true);
        setVerifyState(null);
        try {
            const since = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
            const resp = await fetch(
                `/admin/audit-explorer/verify-chain?since=${encodeURIComponent(since)}&limit=100000`,
                { credentials: 'include', headers: { 'Accept': 'application/json' } },
            );
            if (!resp.ok) {
                setVerifyState({ ok: false, rows: 0, reason: `HTTP ${resp.status}` });
                return;
            }
            const body = await resp.json();
            setVerifyState({
                ok: !!body.continuous,
                rows: body.rows_verified ?? 0,
                reason: body.failure_reason ?? null,
            });
        } catch (err) {
            setVerifyState({ ok: false, rows: 0, reason: `Network error: ${(err as Error).message}` });
        } finally {
            setVerifying(false);
        }
    }

    const totalPages = Math.max(1, Math.ceil(total / per_page));

    function navigate(opts: { page?: number; action_type_prefix?: string }): void {
        const params: Record<string, string | number> = {
            page: opts.page ?? page,
            per_page,
            include_acknowledged: include_acknowledged ? 1 : 0,
        };
        if (filter_workspace_id) params.workspace_id = filter_workspace_id;
        const atp = opts.action_type_prefix ?? actionTypeFilter;
        if (atp) params.action_type_prefix = atp;
        router.get('/admin/alerts-inbox', params, { preserveScroll: true });
    }

    const filtered = items
        .filter(a => severityFilter === 'all' || inferSeverity(a) === severityFilter)
        .filter(a => !actionTypeFilter || a.action_type.includes(actionTypeFilter))
        .sort((a, b) => {
            if (sortBy === 'severity') {
                return (SEVERITY_RANK[inferSeverity(b)] ?? 0) - (SEVERITY_RANK[inferSeverity(a)] ?? 0);
            }
            return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
        });

    // Group counts for the severity chips
    const counts: Record<string, number> = { all: items.length, critical: 0, high: 0, medium: 0, low: 0 };
    for (const a of items) {
        const sev = inferSeverity(a);
        counts[sev] = (counts[sev] ?? 0) + 1;
    }

    async function acknowledge(audit_id: string): Promise<void> {
        setAcking(audit_id);
        setError(null);
        try {
            const csrf = (document.querySelector('meta[name="csrf-token"]') as HTMLMetaElement | null)?.content ?? '';
            const resp = await fetch('/admin/alerts-inbox/acknowledge', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'X-CSRF-TOKEN': csrf,
                },
                body: JSON.stringify({ audit_id, actor_id: 1 }),
            });
            if (!resp.ok) {
                const body = await resp.json();
                setError(body.error ?? 'Acknowledge failed.');
                return;
            }
            router.reload({ only: ['items'] });
        } catch (err) {
            setError(`Network error: ${(err as Error).message}`);
        } finally {
            setAcking(null);
        }
    }

    return (
        <AppLayout>
            <Head title="Alerts Inbox" />
            <div className="px-6 py-4">
                <h1 className="text-2xl font-semibold mb-2">Alerts Inbox</h1>
                <p className="text-sm text-gray-600 mb-3">
                    Audit-anchored alerts (<code>*.alert</code>) — cost burn,
                    vLLM security gate, ingestion gate breaches. Acknowledging
                    writes a <code>*.alert.acknowledged</code> counter row;
                    both are immutable. {items.length} pending.
                </p>

                <div className="mb-3 flex items-center gap-2">
                    <button
                        type="button"
                        onClick={verifyChain}
                        disabled={verifying}
                        className="px-3 py-1 text-xs bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:bg-gray-300"
                        title="Walk audit.audit_ledger for the last 24h and confirm hash-chain integrity"
                    >
                        {verifying ? 'Verifying chain…' : 'Verify audit chain (24h)'}
                    </button>
                    {verifyState && (
                        <span className={`text-xs ${verifyState.ok ? 'text-green-700' : 'text-red-700'}`}>
                            {verifyState.ok
                                ? `✓ Chain continuous across ${verifyState.rows} rows`
                                : `✗ Break detected: ${verifyState.reason ?? 'unknown'}`}
                        </span>
                    )}
                </div>

                {fastapi_error && (
                    <div className="mb-3 p-3 bg-red-50 text-red-800 text-sm rounded">{fastapi_error}</div>
                )}
                {error && (
                    <div className="mb-3 p-3 bg-red-50 text-red-800 text-sm rounded">{error}</div>
                )}

                <div className="mb-3 flex flex-wrap items-center gap-2">
                    <label className="text-sm">
                        <input
                            type="checkbox"
                            className="mr-1"
                            checked={include_acknowledged}
                            onChange={e => router.get('/admin/alerts-inbox', { include_acknowledged: e.target.checked ? 1 : 0 })}
                        />
                        Show acknowledged
                    </label>

                    {/* Severity chips — click to filter */}
                    <div className="flex items-center gap-1 ml-3 text-xs">
                        <span className="text-gray-600">Severity:</span>
                        {(['all', 'critical', 'high', 'medium', 'low'] as Severity[]).map(sev => {
                            const isActive = severityFilter === sev;
                            const sevCls =
                                sev === 'critical' ? 'border-red-300' :
                                sev === 'high'     ? 'border-amber-300' :
                                sev === 'medium'   ? 'border-yellow-300' :
                                sev === 'low'      ? 'border-slate-300' :
                                                     'border-gray-300';
                            return (
                                <button
                                    key={sev}
                                    type="button"
                                    onClick={() => setSeverityFilter(sev)}
                                    className={`px-2 py-0.5 rounded border ${sevCls} ${isActive ? 'bg-blue-100 font-semibold' : 'bg-white hover:bg-gray-50'}`}
                                >
                                    {sev} <span className="text-gray-500">({counts[sev] ?? 0})</span>
                                </button>
                            );
                        })}
                    </div>

                    {/* Action-type prefix filter — server-side (re-queries) */}
                    <input
                        type="text"
                        placeholder="action_type prefix (e.g. cost.)"
                        className="p-1 border rounded text-xs font-mono w-56"
                        value={actionTypeFilter}
                        onChange={e => setActionTypeFilter(e.target.value)}
                        onKeyDown={e => {
                            if (e.key === 'Enter') {
                                navigate({ page: 1, action_type_prefix: actionTypeFilter });
                            }
                        }}
                    />
                    <button
                        type="button"
                        onClick={() => navigate({ page: 1, action_type_prefix: actionTypeFilter })}
                        className="px-2 py-1 bg-gray-100 border rounded text-xs hover:bg-gray-200"
                    >
                        Apply
                    </button>

                    {/* Sort toggle */}
                    <label className="text-xs ml-auto">
                        Sort:
                        <select
                            className="ml-1 p-1 border rounded text-xs"
                            value={sortBy}
                            onChange={e => setSortBy(e.target.value as 'created_at' | 'severity')}
                        >
                            <option value="created_at">Newest first</option>
                            <option value="severity">Severity</option>
                        </select>
                    </label>
                </div>

                <table className="w-full text-sm border-collapse">
                    <thead>
                        <tr className="bg-gray-50 text-left">
                            <th className="py-2 px-2">When</th>
                            <th className="py-2 px-2">Severity</th>
                            <th className="py-2 px-2">Action type</th>
                            <th className="py-2 px-2">Workspace</th>
                            <th className="py-2 px-2">Target</th>
                            <th className="py-2 px-2 text-right">Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        {items.length === 0 && (
                            <tr>
                                <td colSpan={6} className="py-6 text-center text-gray-500">
                                    No alerts. The inbox is clear.
                                </td>
                            </tr>
                        )}
                        {items.length > 0 && filtered.length === 0 && (
                            <tr>
                                <td colSpan={6} className="py-6 text-center text-gray-500">
                                    No alerts match the active filters.
                                </td>
                            </tr>
                        )}
                        {filtered.map(a => (
                            <>
                                <tr key={a.audit_id} className={`border-b hover:bg-gray-50 cursor-pointer ${a.acknowledged_at ? 'opacity-50' : ''}`}
                                    onClick={() => setExpanded(expanded === a.audit_id ? null : a.audit_id)}>
                                    <td className="py-2 px-2 text-xs text-gray-600 whitespace-nowrap">
                                        {new Date(a.created_at).toLocaleString()}
                                    </td>
                                    <td className="py-2 px-2">{severityBadge(a.action_type, a.severity)}</td>
                                    <td className="py-2 px-2 font-mono text-xs">{a.action_type}</td>
                                    <td className="py-2 px-2 font-mono text-xs">
                                        {a.workspace_id ? a.workspace_id.slice(0, 8) + '…' : '—'}
                                    </td>
                                    <td className="py-2 px-2 font-mono text-xs">
                                        {a.target_schema && a.target_table ? `${a.target_schema}.${a.target_table}` : '—'}
                                        {a.target_id ? <span className="text-gray-500"> · {a.target_id.slice(0, 10)}…</span> : null}
                                    </td>
                                    <td className="py-2 px-2 text-right">
                                        {a.acknowledged_at ? (
                                            <span className="text-xs text-gray-500">
                                                Acked {new Date(a.acknowledged_at).toLocaleDateString()}
                                            </span>
                                        ) : (
                                            <button
                                                type="button"
                                                onClick={e => { e.stopPropagation(); acknowledge(a.audit_id); }}
                                                disabled={acking === a.audit_id}
                                                className="px-2 py-1 bg-blue-600 text-white text-xs rounded hover:bg-blue-700 disabled:bg-gray-300"
                                            >
                                                {acking === a.audit_id ? 'Acking…' : 'Acknowledge'}
                                            </button>
                                        )}
                                    </td>
                                </tr>
                                {expanded === a.audit_id && (
                                    <tr className="bg-gray-50">
                                        <td colSpan={6} className="py-2 px-4">
                                            <pre className="text-xs whitespace-pre-wrap max-h-72 overflow-auto">
                                                {JSON.stringify(a.payload, null, 2)}
                                            </pre>
                                        </td>
                                    </tr>
                                )}
                            </>
                        ))}
                    </tbody>
                </table>

                {/* Pagination — server-side */}
                {totalPages > 1 && (
                    <div className="mt-3 flex items-center justify-between text-sm">
                        <span className="text-gray-600">
                            Page {page} of {totalPages} · {total} alerts total
                        </span>
                        <div className="flex gap-2">
                            <button
                                type="button"
                                onClick={() => navigate({ page: Math.max(1, page - 1) })}
                                disabled={page <= 1}
                                className="px-3 py-1 bg-white border rounded hover:bg-gray-50 disabled:opacity-40"
                            >
                                ← Prev
                            </button>
                            <button
                                type="button"
                                onClick={() => navigate({ page: Math.min(totalPages, page + 1) })}
                                disabled={page >= totalPages}
                                className="px-3 py-1 bg-white border rounded hover:bg-gray-50 disabled:opacity-40"
                            >
                                Next →
                            </button>
                        </div>
                    </div>
                )}
            </div>
        </AppLayout>
    );
}
