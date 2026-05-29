import type { JSX } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

/**
 * /admin/target-recommendation/runs — Phase H4 §8 UI.
 *
 * Lists recent Target Recommendation runs across workspaces.
 * Operators click through to /admin/target-recommendation/runs/{run_id}
 * for the cockpit + R5 sign-off ceremony.
 */

type RunSummary = {
    run_id: string;
    workspace_id: string;
    project_id: string;
    project_name: string | null;
    created_at: string | null;
    target_count: number;
    top_score: number | null;
    sign_off_status: string;
};

type PageProps = {
    runs: RunSummary[];
    fastapi_error: string | null;
};

function statusBadge(status: string): JSX.Element {
    const colour =
        status === 'signed_off' ? 'bg-green-100 text-green-800'
        : status === 'rejected' ? 'bg-red-100 text-red-800'
        : status === 'modified' ? 'bg-amber-100 text-amber-800'
        : 'bg-gray-100 text-gray-800';
    return (
        <span className={`inline-block px-2 py-0.5 rounded text-xs ${colour}`}>
            {status}
        </span>
    );
}

export default function TargetRecommendationRuns({ runs, fastapi_error }: PageProps): JSX.Element {
    // Phase 2 real-time push — score_targets workflow broadcasts to
    // admin.target-recommendation on completion (success or failure).
    useAdminSurfaceUpdated('target-recommendation', null, () => {
        router.reload({ only: ['runs'] });
    });

    return (
        <AppLayout>
            <Head title="Target Recommendation Runs" />
            <div className="px-6 py-4">
                <h1 className="text-2xl font-semibold mb-2">
                    Target Recommendation Runs
                </h1>
                <p className="text-sm text-gray-600 mb-4">
                    §8 Target Recommendation Graph runs awaiting QP review or
                    already signed off. Click a row to enter the cockpit and
                    perform the §29.6 R5 sign-off ceremony.
                </p>

                {fastapi_error && (
                    <div className="mb-3 p-3 bg-red-50 text-red-800 text-sm rounded">
                        Could not reach FastAPI: {fastapi_error}
                    </div>
                )}

                <table className="w-full text-sm border-collapse">
                    <thead>
                        <tr className="bg-gray-50 text-left">
                            <th className="py-2 px-2">Run</th>
                            <th className="py-2 px-2">Project</th>
                            <th className="py-2 px-2 text-right">Targets</th>
                            <th className="py-2 px-2 text-right">Top score</th>
                            <th className="py-2 px-2">Status</th>
                            <th className="py-2 px-2">Created</th>
                        </tr>
                    </thead>
                    <tbody>
                        {runs.length === 0 && (
                            <tr>
                                <td colSpan={6} className="py-6 text-center text-gray-500">
                                    No target recommendation runs yet.
                                </td>
                            </tr>
                        )}
                        {runs.map(r => (
                            <tr key={r.run_id} className="border-b hover:bg-gray-50">
                                <td className="py-2 px-2 font-mono text-xs">
                                    <Link
                                        href={`/admin/target-recommendation/runs/${r.run_id}`}
                                        className="text-blue-600 hover:underline"
                                    >
                                        {r.run_id.slice(0, 8)}…
                                    </Link>
                                </td>
                                <td className="py-2 px-2">
                                    {r.project_name ?? r.project_id.slice(0, 8) + '…'}
                                </td>
                                <td className="py-2 px-2 text-right">{r.target_count}</td>
                                <td className="py-2 px-2 text-right">
                                    {r.top_score != null ? r.top_score.toFixed(3) : '—'}
                                </td>
                                <td className="py-2 px-2">{statusBadge(r.sign_off_status)}</td>
                                <td className="py-2 px-2 text-gray-600 text-xs">
                                    {r.created_at ? new Date(r.created_at).toLocaleString() : '—'}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </AppLayout>
    );
}
