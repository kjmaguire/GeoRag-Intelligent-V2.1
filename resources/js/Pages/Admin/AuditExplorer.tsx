import type { JSX } from 'react';
import { useState } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

type Entry = {
    id: string;
    workspace_id: string | null;
    action_type: string;
    target_schema: string | null;
    target_table: string | null;
    target_id: string | null;
    actor_id: number | null;
    created_at: string;
    payload: Record<string, unknown>;
};

type PageProps = { entries: Entry[]; filters: Record<string, string>; fastapi_error: string | null };

export default function AuditExplorer({ entries, filters, fastapi_error }: PageProps): JSX.Element {
    // Phase 5 real-time push — audit_ledger_verify (cron) broadcasts on
    // completion. The 2 s hook debounce collapses bursts.
    useAdminSurfaceUpdated('audit-explorer', null, () => {
        router.reload({ only: ['entries'] });
    });

    const [actionPrefix, setActionPrefix] = useState<string>(filters.action_type_prefix ?? '');
    const [ws, setWs] = useState<string>(filters.workspace_id ?? '');
    const [targetId, setTargetId] = useState<string>(filters.target_id ?? '');
    const [actorId, setActorId] = useState<string>(filters.actor_id ?? '');
    const [expanded, setExpanded] = useState<string | null>(null);

    function apply(): void {
        const params: Record<string, string> = {};
        if (actionPrefix) params.action_type_prefix = actionPrefix;
        if (ws) params.workspace_id = ws;
        if (targetId) params.target_id = targetId;
        if (actorId) params.actor_id = actorId;
        router.get('/admin/audit-explorer', params);
    }

    return (
        <AppLayout>
            <Head title="Audit Explorer" />
            <div className="px-6 py-4">
                <h1 className="text-2xl font-semibold mb-2">Audit Explorer</h1>
                <p className="text-sm text-gray-600 mb-4">
                    Filter the audit.audit_ledger across all workspaces.
                    Useful for investigating cross-workspace incidents +
                    operator-mode forensics.
                </p>

                {fastapi_error && <div className="mb-3 p-3 bg-red-50 text-red-800 text-sm rounded">{fastapi_error}</div>}

                <div className="mb-4 grid grid-cols-4 gap-2 p-3 border rounded bg-white">
                    <label className="text-sm">Action prefix
                        <input className="block w-full mt-1 p-1.5 border rounded font-mono text-xs"
                               value={actionPrefix} onChange={e => setActionPrefix(e.target.value)} placeholder="report.export" />
                    </label>
                    <label className="text-sm">Workspace
                        <input className="block w-full mt-1 p-1.5 border rounded font-mono text-xs"
                               value={ws} onChange={e => setWs(e.target.value)} />
                    </label>
                    <label className="text-sm">Target id
                        <input className="block w-full mt-1 p-1.5 border rounded font-mono text-xs"
                               value={targetId} onChange={e => setTargetId(e.target.value)} />
                    </label>
                    <label className="text-sm">Actor id
                        <input className="block w-full mt-1 p-1.5 border rounded font-mono text-xs"
                               value={actorId} onChange={e => setActorId(e.target.value)} />
                    </label>
                    <button type="button" onClick={apply} className="col-span-4 px-3 py-1.5 bg-blue-600 text-white rounded hover:bg-blue-700 self-start w-fit">
                        Search
                    </button>
                </div>

                <table className="w-full text-sm border-collapse">
                    <thead>
                        <tr className="bg-gray-50 text-left">
                            <th className="py-2 px-2">Action</th>
                            <th className="py-2 px-2">Workspace</th>
                            <th className="py-2 px-2">Target</th>
                            <th className="py-2 px-2">Actor</th>
                            <th className="py-2 px-2">When</th>
                        </tr>
                    </thead>
                    <tbody>
                        {entries.length === 0 && <tr><td colSpan={5} className="py-6 text-center text-gray-500">No results.</td></tr>}
                        {entries.map(e => (
                            <>
                                <tr key={e.id} className="border-b hover:bg-gray-50 cursor-pointer"
                                    onClick={() => setExpanded(expanded === e.id ? null : e.id)}>
                                    <td className="py-2 px-2 font-mono text-xs">{e.action_type}</td>
                                    <td className="py-2 px-2 font-mono text-xs">
                                        {e.workspace_id ? e.workspace_id.slice(0, 8) + '…' : 'system'}
                                    </td>
                                    <td className="py-2 px-2 text-xs">
                                        {e.target_schema && e.target_table ? `${e.target_schema}.${e.target_table}` : '—'}
                                        {e.target_id && <span className="text-gray-500 ml-2 font-mono">[{e.target_id.slice(0, 8)}…]</span>}
                                    </td>
                                    <td className="py-2 px-2">{e.actor_id ?? 'system'}</td>
                                    <td className="py-2 px-2 text-xs text-gray-600">{new Date(e.created_at).toLocaleString()}</td>
                                </tr>
                                {expanded === e.id && (
                                    <tr key={e.id + '-detail'} className="bg-gray-50">
                                        <td colSpan={5} className="py-2 px-4">
                                            <pre className="text-xs whitespace-pre-wrap max-h-96 overflow-auto">
                                                {JSON.stringify(e.payload, null, 2)}
                                            </pre>
                                        </td>
                                    </tr>
                                )}
                            </>
                        ))}
                    </tbody>
                </table>
            </div>
        </AppLayout>
    );
}
