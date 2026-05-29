import type { JSX } from 'react';
import { useState } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

/**
 * /admin/backups — operator surface for §11.1 backup crons + §11.10 cold-tier.
 *
 * Two tables:
 *   - Snapshot runs across all 5 stores (PG/Neo4j/Qdrant/Redis/SeaweedFS)
 *     with store + status filters + pagination
 *   - Cold-tier archive runs (audit-anchored, no dedicated table)
 *
 * Trigger buttons NOT included here — backup workflows are cron-only;
 * manual triggers go through Hatchet's own dashboard. This page is read-only.
 */

type SnapshotRun = {
    run_id: string;
    store: string;
    started_at: string;
    completed_at: string | null;
    bucket: string | null;
    object_key: string | null;
    sha256_hex: string | null;
    bytes: number | null;
    status: string;
    failure_reason: string | null;
    payload: Record<string, unknown>;
};

type ColdTierRun = {
    audit_id: string;
    action_type: string;
    rows_archived: number;
    cold_tier_uri: string;
    hot_tier_remaining: number | null;
    verification_passed: boolean;
    manifest_key: string | null;
    duration_s: number | null;
    created_at: string;
    payload: Record<string, unknown>;
};

type PageProps = {
    snapshots: SnapshotRun[];
    snapshots_total: number;
    cold_tier_runs: ColdTierRun[];
    page: number;
    per_page: number;
    filter_store: string | null;
    filter_status: string | null;
    fastapi_error: string | null;
};

const STORES = ['', 'postgres', 'neo4j', 'qdrant', 'redis', 'seaweedfs'];
const STATUSES = ['', 'completed', 'running', 'failed'];

function fmtBytes(n: number | null): string {
    if (n == null) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

export default function BackupsDashboard({
    snapshots, snapshots_total, cold_tier_runs,
    page, per_page, filter_store, filter_status, fastapi_error,
}: PageProps): JSX.Element {
    // Phase 5 real-time push — backup_postgres / backup_neo4j / backup_qdrant /
    // backup_redis / backup_seaweedfs all broadcast `backups` on completion.
    // cold_tier_archive (Phase 2 wired) also fires `backups` to refresh the
    // cold_tier_runs panel.
    useAdminSurfaceUpdated('backups', null, () => {
        router.reload({ only: ['snapshots', 'snapshots_total', 'cold_tier_runs'] });
    });

    const [store, setStore] = useState<string>(filter_store ?? '');
    const [status, setStatus] = useState<string>(filter_status ?? '');
    const totalPages = Math.max(1, Math.ceil(snapshots_total / per_page));

    function navigate(opts: { page?: number }): void {
        const params: Record<string, string | number> = {
            page: opts.page ?? page,
            per_page,
        };
        if (store) params.store = store;
        if (status) params.status = status;
        router.get('/admin/backups', params, { preserveScroll: true });
    }

    return (
        <AppLayout>
            <Head title="Backups + Cold-tier" />
            <div className="px-6 py-4">
                <h1 className="text-2xl font-semibold mb-2">Backups + Cold-tier</h1>
                <p className="text-sm text-gray-600 mb-4">
                    §11.1 nightly backup crons + §11.10 audit cold-tier archival.
                    Cron schedule (UTC): PG@02:00 · Neo4j@02:15 · Qdrant@02:30 ·
                    Redis@02:45 · SeaweedFS@03:00 · Cold-tier@04:00. Read-only —
                    manual triggers go through the Hatchet dashboard.
                </p>

                {fastapi_error && (
                    <div className="mb-3 p-3 bg-red-50 border border-red-200 text-red-800 text-sm rounded">
                        {fastapi_error}
                    </div>
                )}

                <h2 className="text-lg font-semibold mt-2 mb-2">Snapshot runs</h2>
                <div className="mb-3 flex items-center gap-2 text-sm">
                    <label>store
                        <select value={store} onChange={e => setStore(e.target.value)}
                                className="ml-1 p-1 border rounded text-xs">
                            {STORES.map(s => <option key={s} value={s}>{s || 'all'}</option>)}
                        </select>
                    </label>
                    <label>status
                        <select value={status} onChange={e => setStatus(e.target.value)}
                                className="ml-1 p-1 border rounded text-xs">
                            {STATUSES.map(s => <option key={s} value={s}>{s || 'all'}</option>)}
                        </select>
                    </label>
                    <button type="button"
                            onClick={() => navigate({ page: 1 })}
                            className="px-2 py-1 bg-gray-100 border rounded text-xs hover:bg-gray-200">
                        Apply
                    </button>
                </div>

                <table className="w-full text-sm border-collapse">
                    <thead>
                        <tr className="bg-gray-50 text-left">
                            <th className="py-2 px-2">When</th>
                            <th className="py-2 px-2">Store</th>
                            <th className="py-2 px-2">Status</th>
                            <th className="py-2 px-2">Bucket / key</th>
                            <th className="py-2 px-2 text-right">Bytes</th>
                            <th className="py-2 px-2">SHA-256 (12)</th>
                        </tr>
                    </thead>
                    <tbody>
                        {snapshots.length === 0 && (
                            <tr><td colSpan={6} className="py-6 text-center text-gray-500">
                                No snapshot runs yet. First cron fires at 02:00 UTC.
                            </td></tr>
                        )}
                        {snapshots.map(s => (
                            <tr key={s.run_id} className="border-b hover:bg-gray-50">
                                <td className="py-2 px-2 text-xs text-gray-600 whitespace-nowrap">
                                    {new Date(s.started_at).toLocaleString()}
                                </td>
                                <td className="py-2 px-2 font-mono text-xs">{s.store}</td>
                                <td className="py-2 px-2">
                                    {s.status === 'completed' && <span className="text-green-700">✓ {s.status}</span>}
                                    {s.status === 'failed' && <span className="text-red-700" title={s.failure_reason ?? ''}>✗ {s.status}</span>}
                                    {s.status === 'running' && <span className="text-blue-700">⏳ {s.status}</span>}
                                </td>
                                <td className="py-2 px-2 font-mono text-xs text-gray-600">
                                    {s.bucket ? `${s.bucket}/${s.object_key ?? ''}` : '—'}
                                </td>
                                <td className="py-2 px-2 text-right text-xs font-mono">{fmtBytes(s.bytes)}</td>
                                <td className="py-2 px-2 font-mono text-xs">
                                    {s.sha256_hex ? s.sha256_hex.slice(0, 12) : '—'}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>

                {totalPages > 1 && (
                    <div className="mt-3 flex items-center justify-between text-sm">
                        <span className="text-gray-600">Page {page} of {totalPages} · {snapshots_total} runs</span>
                        <div className="flex gap-2">
                            <button type="button" disabled={page <= 1}
                                    onClick={() => navigate({ page: Math.max(1, page - 1) })}
                                    className="px-3 py-1 bg-white border rounded hover:bg-gray-50 disabled:opacity-40">← Prev</button>
                            <button type="button" disabled={page >= totalPages}
                                    onClick={() => navigate({ page: Math.min(totalPages, page + 1) })}
                                    className="px-3 py-1 bg-white border rounded hover:bg-gray-50 disabled:opacity-40">Next →</button>
                        </div>
                    </div>
                )}

                <h2 className="text-lg font-semibold mt-8 mb-2">Cold-tier archive runs</h2>
                <p className="text-xs text-gray-500 mb-2">
                    Nightly cron at 04:00 UTC archives audit.audit_ledger rows older
                    than 90 days into the <code>audit-cold-tier</code> SeaweedFS bucket.
                    Pruning is operator-gated (NOT automatic).
                </p>
                <table className="w-full text-sm border-collapse">
                    <thead>
                        <tr className="bg-gray-50 text-left">
                            <th className="py-2 px-2">When</th>
                            <th className="py-2 px-2">Result</th>
                            <th className="py-2 px-2 text-right">Rows</th>
                            <th className="py-2 px-2 text-right">Hot remaining</th>
                            <th className="py-2 px-2">Cold-tier URI</th>
                            <th className="py-2 px-2 text-right">Duration</th>
                        </tr>
                    </thead>
                    <tbody>
                        {cold_tier_runs.length === 0 && (
                            <tr><td colSpan={6} className="py-6 text-center text-gray-500">
                                No cold-tier runs yet. First cron fires at 04:00 UTC.
                            </td></tr>
                        )}
                        {cold_tier_runs.map(r => (
                            <tr key={r.audit_id} className="border-b hover:bg-gray-50">
                                <td className="py-2 px-2 text-xs text-gray-600 whitespace-nowrap">
                                    {new Date(r.created_at).toLocaleString()}
                                </td>
                                <td className="py-2 px-2">
                                    {r.verification_passed
                                        ? <span className="text-green-700">✓ verified</span>
                                        : <span className="text-red-700">✗ chain break</span>}
                                </td>
                                <td className="py-2 px-2 text-right font-mono text-xs">{r.rows_archived.toLocaleString()}</td>
                                <td className="py-2 px-2 text-right font-mono text-xs">
                                    {r.hot_tier_remaining?.toLocaleString() ?? '—'}
                                </td>
                                <td className="py-2 px-2 font-mono text-xs text-gray-600 truncate max-w-md">
                                    {r.cold_tier_uri || '—'}
                                </td>
                                <td className="py-2 px-2 text-right text-xs font-mono">
                                    {r.duration_s != null ? `${r.duration_s.toFixed(1)}s` : '—'}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </AppLayout>
    );
}
