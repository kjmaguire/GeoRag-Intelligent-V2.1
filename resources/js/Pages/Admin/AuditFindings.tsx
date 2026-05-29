import type { JSX } from 'react';
import { useState } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

/**
 * /admin/audit — combined audit findings.
 *   §11.5 Tenant Isolation auditor live state
 *   §11.10 audit cold-tier archival runs (+ dry-run trigger)
 *   §6.4 boundary language violations
 */

type Finding = {
    schema: string;
    table: string;
    gate: string;
    detail: string;
};

type TenantReport = {
    findings: Finding[];
    total: number;
    auditor_clean: boolean;
};

type Run = {
    run_id: string;
    workspace_id: string | null;
    created_at: string;
    payload: Record<string, unknown>;
};

type Violation = {
    audit_id: string;
    workspace_id: string | null;
    created_at: string;
    payload: Record<string, unknown>;
};

type PageProps = {
    tenant_isolation: TenantReport | null;
    archive_runs: Run[];
    boundary_violations: Violation[];
    fastapi_error: string | null;
};

const GATE_BADGE: Record<string, string> = {
    workspace_id: 'bg-red-100 text-red-800',
    rls_enabled:  'bg-amber-100 text-amber-800',
    policy:       'bg-orange-100 text-orange-800',
    index:        'bg-gray-100 text-gray-800',
    fk:           'bg-indigo-100 text-indigo-800',
};

export default function AuditFindings(props: PageProps): JSX.Element {
    const { tenant_isolation, archive_runs, boundary_violations, fastapi_error } = props;

    // Phase 2 real-time push — cold_tier_archive broadcasts to
    // admin.audit-findings on completion (success or failure). Refreshes
    // the archive_runs panel; tenant_isolation + boundary_violations are
    // periodically-evaluated, no live writer, so they stay on the page-
    // load snapshot.
    useAdminSurfaceUpdated('audit-findings', null, () => {
        router.reload({ only: ['archive_runs'] });
    });

    const [cutoffISO, setCutoffISO] = useState<string>(() => {
        // default cutoff: 90 days ago
        const d = new Date();
        d.setDate(d.getDate() - 90);
        return d.toISOString().slice(0, 16);
    });
    const [dryRun, setDryRun] = useState<boolean>(true);
    const [busy, setBusy] = useState<boolean>(false);
    const [archiveResult, setArchiveResult] = useState<Record<string, unknown> | null>(null);
    const [error, setError] = useState<string | null>(null);

    async function triggerArchive(): Promise<void> {
        setBusy(true);
        setError(null);
        setArchiveResult(null);
        try {
            const csrf = (document.querySelector('meta[name="csrf-token"]') as HTMLMetaElement | null)?.content ?? '';
            const resp = await fetch('/admin/audit/cold-tier-archive', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'X-CSRF-TOKEN': csrf,
                },
                body: JSON.stringify({
                    cutoff_before_iso: new Date(cutoffISO).toISOString(),
                    dry_run: dryRun,
                }),
            });
            const body = await resp.json();
            if (resp.ok) {
                setArchiveResult(body);
                router.reload({ only: ['archive_runs'] });
            } else {
                setError(body.error ?? 'Trigger failed.');
            }
        } catch (err) {
            setError(`Network error: ${(err as Error).message}`);
        } finally {
            setBusy(false);
        }
    }

    return (
        <AppLayout>
            <Head title="Audit Findings" />
            <div className="px-6 py-4">
                <h1 className="text-2xl font-semibold mb-2">Audit Findings</h1>
                <p className="text-sm text-gray-600 mb-4">
                    Combined view of §11.5 tenant-isolation auditor + §11.10
                    cold-tier archival + §6.4 public/private boundary
                    language violations.
                </p>

                {fastapi_error && (
                    <div className="mb-3 p-3 bg-red-50 text-red-800 text-sm rounded">
                        Could not reach FastAPI: {fastapi_error}
                    </div>
                )}

                {/* Tenant Isolation */}
                <section className="mb-8">
                    <div className="flex justify-between items-baseline mb-2">
                        <h2 className="text-lg font-semibold">§11.5 Tenant Isolation</h2>
                        {tenant_isolation && (
                            <span className={`text-sm font-medium ${
                                tenant_isolation.auditor_clean ? 'text-green-700' : 'text-red-700'
                            }`}>
                                {tenant_isolation.auditor_clean
                                    ? '✓ Auditor clean'
                                    : `${tenant_isolation.total} finding(s)`}
                            </span>
                        )}
                    </div>
                    {tenant_isolation?.findings.length === 0 ? (
                        <p className="text-sm text-gray-600">No tenant-isolation findings.</p>
                    ) : (
                        <table className="w-full text-sm border-collapse">
                            <thead>
                                <tr className="bg-gray-50 text-left">
                                    <th className="py-2 px-2">Table</th>
                                    <th className="py-2 px-2">Gate</th>
                                    <th className="py-2 px-2">Detail</th>
                                </tr>
                            </thead>
                            <tbody>
                                {tenant_isolation?.findings.map((f, i) => (
                                    <tr key={i} className="border-b">
                                        <td className="py-2 px-2 font-mono text-xs">{f.schema}.{f.table}</td>
                                        <td className="py-2 px-2">
                                            <span className={`inline-block px-2 py-0.5 rounded text-xs ${
                                                GATE_BADGE[f.gate] ?? 'bg-gray-100 text-gray-700'
                                            }`}>
                                                {f.gate}
                                            </span>
                                        </td>
                                        <td className="py-2 px-2 text-gray-700 text-xs">{f.detail}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    )}
                </section>

                {/* Cold-Tier Archive */}
                <section className="mb-8">
                    <h2 className="text-lg font-semibold mb-2">§11.10 Audit Cold-Tier Archive</h2>
                    <div className="p-3 border rounded bg-white mb-3">
                        <div className="grid grid-cols-3 gap-3">
                            <label className="text-sm">
                                Cutoff (rows older than)
                                <input
                                    type="datetime-local"
                                    className="block w-full mt-1 p-1.5 border rounded"
                                    value={cutoffISO}
                                    onChange={e => setCutoffISO(e.target.value)}
                                />
                            </label>
                            <label className="text-sm flex items-center mt-5">
                                <input
                                    type="checkbox"
                                    checked={dryRun}
                                    onChange={e => setDryRun(e.target.checked)}
                                    className="mr-2"
                                />
                                Dry run (default)
                            </label>
                            <button
                                type="button"
                                onClick={triggerArchive}
                                disabled={busy}
                                className="self-end px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:bg-gray-300"
                            >
                                {busy ? 'Running…' : 'Trigger archive'}
                            </button>
                        </div>
                        {error && (
                            <div className="mt-2 p-2 bg-red-50 text-red-800 text-sm rounded">{error}</div>
                        )}
                        {archiveResult && (
                            <pre className="mt-2 p-2 bg-gray-50 text-xs rounded max-h-40 overflow-auto">
                                {JSON.stringify(archiveResult, null, 2)}
                            </pre>
                        )}
                    </div>

                    <table className="w-full text-sm border-collapse">
                        <thead>
                            <tr className="bg-gray-50 text-left">
                                <th className="py-2 px-2">Run</th>
                                <th className="py-2 px-2">Workspace</th>
                                <th className="py-2 px-2">Cutoff</th>
                                <th className="py-2 px-2 text-right">Rows archived</th>
                                <th className="py-2 px-2">When</th>
                            </tr>
                        </thead>
                        <tbody>
                            {archive_runs.length === 0 && (
                                <tr>
                                    <td colSpan={5} className="py-4 text-center text-gray-500">
                                        No archive runs recorded yet.
                                    </td>
                                </tr>
                            )}
                            {archive_runs.map(r => (
                                <tr key={r.run_id} className="border-b">
                                    <td className="py-2 px-2 font-mono text-xs">{r.run_id.slice(0, 8)}…</td>
                                    <td className="py-2 px-2 font-mono text-xs">
                                        {r.workspace_id ? r.workspace_id.slice(0, 8) + '…' : 'global'}
                                    </td>
                                    <td className="py-2 px-2 text-xs text-gray-600">
                                        {String((r.payload as Record<string, unknown>).cutoff_before ?? '—')}
                                    </td>
                                    <td className="py-2 px-2 text-right">
                                        {String((r.payload as Record<string, unknown>).rows_archived ?? '—')}
                                    </td>
                                    <td className="py-2 px-2 text-xs text-gray-600">
                                        {new Date(r.created_at).toLocaleString()}
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </section>

                {/* Boundary Violations */}
                <section className="mb-8">
                    <h2 className="text-lg font-semibold mb-2">§6.4 Public/Private Boundary Violations</h2>
                    <table className="w-full text-sm border-collapse">
                        <thead>
                            <tr className="bg-gray-50 text-left">
                                <th className="py-2 px-2">Audit id</th>
                                <th className="py-2 px-2">Workspace</th>
                                <th className="py-2 px-2">Detail</th>
                                <th className="py-2 px-2">When</th>
                            </tr>
                        </thead>
                        <tbody>
                            {boundary_violations.length === 0 && (
                                <tr>
                                    <td colSpan={4} className="py-4 text-center text-gray-500">
                                        No boundary violations recorded.
                                    </td>
                                </tr>
                            )}
                            {boundary_violations.map(v => (
                                <tr key={v.audit_id} className="border-b">
                                    <td className="py-2 px-2 font-mono text-xs">{v.audit_id.slice(0, 8)}…</td>
                                    <td className="py-2 px-2 font-mono text-xs">
                                        {v.workspace_id ? v.workspace_id.slice(0, 8) + '…' : '—'}
                                    </td>
                                    <td className="py-2 px-2 text-xs text-gray-600 truncate max-w-xl">
                                        {JSON.stringify(v.payload).slice(0, 200)}
                                    </td>
                                    <td className="py-2 px-2 text-xs text-gray-600">
                                        {new Date(v.created_at).toLocaleString()}
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </section>
            </div>
        </AppLayout>
    );
}
