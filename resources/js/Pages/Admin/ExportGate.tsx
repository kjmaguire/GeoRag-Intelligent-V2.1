import type { JSX } from 'react';
import { useState } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

/**
 * /admin/export-gate — §29 export compliance gate results.
 *
 * Surfaces the 10 G01–G10 gate decisions per export. Click a row to
 * see the full structured payload (gate-by-gate pass/fail breakdown).
 */

type Result = {
    audit_id: string;
    workspace_id: string | null;
    target_id: string | null;
    action_type: string;
    created_at: string;
    payload: Record<string, unknown>;
};

type PageProps = { results: Result[]; fastapi_error: string | null };

function statusBadge(action_type: string, payload: Record<string, unknown>): JSX.Element {
    const all_pass = Boolean(payload.all_gates_passed ?? payload.passed);
    const explicit_fail = action_type.endsWith('.failed');
    const colour = all_pass && !explicit_fail
        ? 'bg-green-100 text-green-800'
        : 'bg-red-100 text-red-800';
    return (
        <span className={`inline-block px-2 py-0.5 rounded text-xs ${colour}`}>
            {all_pass && !explicit_fail ? 'pass' : 'fail'}
        </span>
    );
}

export default function ExportGate({ results, fastapi_error }: PageProps): JSX.Element {
    // Phase 5 real-time push — workspace_export + outbox_dispatcher both
    // broadcast `export-gate` on completion.
    useAdminSurfaceUpdated('export-gate', null, () => {
        router.reload({ only: ['results'] });
    });

    const [expanded, setExpanded] = useState<string | null>(null);

    return (
        <AppLayout>
            <Head title="Export Compliance Gates" />
            <div className="px-6 py-4">
                <h1 className="text-2xl font-semibold mb-2">Export Compliance Gates</h1>
                <p className="text-sm text-gray-600 mb-4">
                    §29 export compliance decisions. Each row is one
                    gate run; the payload carries per-gate G01–G10
                    pass/fail breakdown.
                </p>

                {fastapi_error && (
                    <div className="mb-3 p-3 bg-red-50 text-red-800 text-sm rounded">
                        Could not reach FastAPI: {fastapi_error}
                    </div>
                )}

                <table className="w-full text-sm border-collapse">
                    <thead>
                        <tr className="bg-gray-50 text-left">
                            <th className="py-2 px-2">Action</th>
                            <th className="py-2 px-2">Status</th>
                            <th className="py-2 px-2">Workspace</th>
                            <th className="py-2 px-2">Target</th>
                            <th className="py-2 px-2">When</th>
                        </tr>
                    </thead>
                    <tbody>
                        {results.length === 0 && (
                            <tr>
                                <td colSpan={5} className="py-6 text-center text-gray-500">
                                    No export gate results recorded yet.
                                </td>
                            </tr>
                        )}
                        {results.map(r => (
                            <>
                                <tr
                                    key={r.audit_id}
                                    className="border-b hover:bg-gray-50 cursor-pointer"
                                    onClick={() => setExpanded(expanded === r.audit_id ? null : r.audit_id)}
                                >
                                    <td className="py-2 px-2 font-mono text-xs">{r.action_type}</td>
                                    <td className="py-2 px-2">{statusBadge(r.action_type, r.payload)}</td>
                                    <td className="py-2 px-2 font-mono text-xs">
                                        {r.workspace_id ? r.workspace_id.slice(0, 8) + '…' : '—'}
                                    </td>
                                    <td className="py-2 px-2 font-mono text-xs">
                                        {r.target_id ? r.target_id.slice(0, 16) + '…' : '—'}
                                    </td>
                                    <td className="py-2 px-2 text-xs text-gray-600">
                                        {new Date(r.created_at).toLocaleString()}
                                    </td>
                                </tr>
                                {expanded === r.audit_id && (
                                    <tr key={r.audit_id + '-detail'} className="bg-gray-50">
                                        <td colSpan={5} className="py-2 px-4">
                                            <pre className="text-xs whitespace-pre-wrap max-h-72 overflow-auto">
                                                {JSON.stringify(r.payload, null, 2)}
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
