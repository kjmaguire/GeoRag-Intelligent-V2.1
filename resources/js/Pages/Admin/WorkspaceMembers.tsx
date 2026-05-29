import type { JSX } from 'react';
import { useState } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';

type Member = {
    workspace_id: string;
    user_id: number;
    user_name: string | null;
    user_email: string | null;
    role: string;
    granted_at: string | null;
};

type PageProps = { members: Member[]; fastapi_error: string | null; filter_workspace_id: string | null };

export default function WorkspaceMembers({ members, fastapi_error, filter_workspace_id }: PageProps): JSX.Element {
    const [filter, setFilter] = useState<string>(filter_workspace_id ?? '');

    return (
        <AppLayout>
            <Head title="Workspace Members" />
            <div className="px-6 py-4">
                <h1 className="text-2xl font-semibold mb-2">Workspace Members</h1>
                <p className="text-sm text-gray-600 mb-4">
                    Cross-workspace view of silver.user_workspace_grants.
                    Use to audit role assignments and recent grants.
                </p>

                {fastapi_error && <div className="mb-3 p-3 bg-red-50 text-red-800 text-sm rounded">{fastapi_error}</div>}

                <div className="mb-3 flex gap-2 items-baseline">
                    <input className="p-1.5 border rounded font-mono text-xs w-80" placeholder="Filter workspace UUID"
                           value={filter} onChange={e => setFilter(e.target.value)} />
                    <button type="button" onClick={() => router.get('/admin/workspace-members', filter ? { workspace_id: filter } : {})}
                            className="px-3 py-1.5 bg-blue-600 text-white rounded hover:bg-blue-700">Apply</button>
                </div>

                <table className="w-full text-sm border-collapse">
                    <thead>
                        <tr className="bg-gray-50 text-left">
                            <th className="py-2 px-2">Workspace</th>
                            <th className="py-2 px-2">User</th>
                            <th className="py-2 px-2">Email</th>
                            <th className="py-2 px-2">Role</th>
                            <th className="py-2 px-2">Granted</th>
                        </tr>
                    </thead>
                    <tbody>
                        {members.length === 0 && <tr><td colSpan={5} className="py-6 text-center text-gray-500">No members.</td></tr>}
                        {members.map(m => (
                            <tr key={`${m.workspace_id}-${m.user_id}`} className="border-b hover:bg-gray-50">
                                <td className="py-2 px-2 font-mono text-xs">{m.workspace_id.slice(0, 8)}…</td>
                                <td className="py-2 px-2">{m.user_name ?? `#${m.user_id}`}</td>
                                <td className="py-2 px-2 text-xs">{m.user_email ?? '—'}</td>
                                <td className="py-2 px-2"><span className="px-2 py-0.5 rounded text-xs bg-gray-100">{m.role}</span></td>
                                <td className="py-2 px-2 text-xs text-gray-600">
                                    {m.granted_at ? new Date(m.granted_at).toLocaleString() : '—'}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </AppLayout>
    );
}
