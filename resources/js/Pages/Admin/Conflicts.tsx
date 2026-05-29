import type { JSX } from 'react';
import { useState } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

/**
 * /admin/conflicts — §7.4 Conflict Resolver review queue.
 *
 * Two surfaces:
 *   - Recent conflict-related audit entries
 *   - Test bench: paste claims JSON → run resolver → see result
 */

type AuditEntry = {
    id: string;
    workspace_id: string | null;
    action_type: string;
    created_at: string;
    target_id: string | null;
    payload: Record<string, unknown>;
};

type PageProps = {
    entries: AuditEntry[];
    fastapi_error: string | null;
};

const EXAMPLE_CLAIMS = JSON.stringify(
    [
        {
            claim_id: 'c1',
            text: 'Total depth of hole PLS-22-08 is 339 metres.',
            validated: true,
            evidence: [
                { source_chunk_id: 'e1', is_stale: false, raw_text: 'TD 339 m' },
            ],
        },
        {
            claim_id: 'c2',
            text: 'Total depth of hole PLS-22-08 is 510 metres.',
            validated: true,
            evidence: [
                { source_chunk_id: 'e2', is_stale: false, raw_text: 'TD 510 m' },
            ],
        },
    ],
    null,
    2,
);

export default function Conflicts({ entries, fastapi_error }: PageProps): JSX.Element {
    // Phase 5 real-time push — ConflictsController::run dispatches on every
    // successful POST. New entries surface without manual reload.
    useAdminSurfaceUpdated('conflicts', null, () => {
        router.reload({ only: ['entries'] });
    });

    const [workspaceId, setWorkspaceId] = useState<string>('a0000000-0000-0000-0000-000000000001');
    const [claimsJson, setClaimsJson] = useState<string>(EXAMPLE_CLAIMS);
    const [result, setResult] = useState<Record<string, unknown> | null>(null);
    const [busy, setBusy] = useState<boolean>(false);
    const [error, setError] = useState<string | null>(null);

    async function runResolver(): Promise<void> {
        setBusy(true);
        setError(null);
        setResult(null);
        try {
            const claims = JSON.parse(claimsJson);
            const csrf = (document.querySelector('meta[name="csrf-token"]') as HTMLMetaElement | null)?.content ?? '';
            const resp = await fetch('/admin/conflicts/run', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'X-CSRF-TOKEN': csrf,
                },
                body: JSON.stringify({
                    workspace_id: workspaceId,
                    section_id: 'admin-test-bench',
                    claims,
                }),
            });
            const body = await resp.json();
            if (resp.ok) {
                setResult(body);
            } else {
                setError(body.error ?? 'Run failed.');
            }
        } catch (err) {
            setError(`Parse/network error: ${(err as Error).message}`);
        } finally {
            setBusy(false);
        }
    }

    return (
        <AppLayout>
            <Head title="Conflict Resolver" />
            <div className="px-6 py-4">
                <h1 className="text-2xl font-semibold mb-2">Conflict Resolver</h1>
                <p className="text-sm text-gray-600 mb-4">
                    §7.4 Conflict Resolver Agent — detects value mismatches,
                    freshness drift, and missing-provenance violations across a
                    section's claim ledger. Recent runs are surfaced from
                    audit.audit_ledger; the test bench below invokes the agent
                    against caller-supplied claims.
                </p>

                {fastapi_error && (
                    <div className="mb-3 p-3 bg-red-50 text-red-800 text-sm rounded">
                        Could not reach FastAPI: {fastapi_error}
                    </div>
                )}

                <div className="grid grid-cols-2 gap-4">
                    <div>
                        <h2 className="text-lg font-semibold mb-2">Test bench</h2>
                        <label className="text-sm block mb-2">
                            Workspace id
                            <input
                                type="text"
                                className="block w-full mt-1 p-1.5 border rounded font-mono text-xs"
                                value={workspaceId}
                                onChange={e => setWorkspaceId(e.target.value)}
                            />
                        </label>
                        <label className="text-sm block">
                            Claims (JSON array)
                            <textarea
                                className="block w-full mt-1 p-1.5 border rounded font-mono text-xs"
                                rows={14}
                                value={claimsJson}
                                onChange={e => setClaimsJson(e.target.value)}
                            />
                        </label>
                        <button
                            type="button"
                            onClick={runResolver}
                            disabled={busy}
                            className="mt-2 px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:bg-gray-300"
                        >
                            {busy ? 'Running…' : 'Run resolver'}
                        </button>
                        {error && (
                            <div className="mt-2 p-2 bg-red-50 text-red-800 text-sm rounded">{error}</div>
                        )}
                    </div>

                    <div>
                        <h2 className="text-lg font-semibold mb-2">Result</h2>
                        {result ? (
                            <pre className="p-3 bg-gray-50 rounded text-xs overflow-auto max-h-[60vh] whitespace-pre-wrap">
                                {JSON.stringify(result, null, 2)}
                            </pre>
                        ) : (
                            <p className="text-gray-500 text-sm">
                                Run the resolver to see proposed conflicts.
                            </p>
                        )}
                    </div>
                </div>

                <h2 className="text-lg font-semibold mt-8 mb-2">Recent conflict-related audit entries</h2>
                <table className="w-full text-sm border-collapse">
                    <thead>
                        <tr className="bg-gray-50 text-left">
                            <th className="py-2 px-2">Action</th>
                            <th className="py-2 px-2">Workspace</th>
                            <th className="py-2 px-2">Target</th>
                            <th className="py-2 px-2">When</th>
                        </tr>
                    </thead>
                    <tbody>
                        {entries.length === 0 && (
                            <tr>
                                <td colSpan={4} className="py-4 text-center text-gray-500">
                                    No conflict-related audit entries yet.
                                </td>
                            </tr>
                        )}
                        {entries.map(e => (
                            <tr key={e.id} className="border-b hover:bg-gray-50">
                                <td className="py-2 px-2 font-mono text-xs">{e.action_type}</td>
                                <td className="py-2 px-2 font-mono text-xs">
                                    {e.workspace_id ? e.workspace_id.slice(0, 8) + '…' : 'system'}
                                </td>
                                <td className="py-2 px-2 font-mono text-xs">
                                    {e.target_id ? e.target_id.slice(0, 16) + '…' : '—'}
                                </td>
                                <td className="py-2 px-2 text-xs text-gray-600">
                                    {new Date(e.created_at).toLocaleString()}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </AppLayout>
    );
}
