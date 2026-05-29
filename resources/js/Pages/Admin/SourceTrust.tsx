import type { JSX } from 'react';
import { Head, router } from '@inertiajs/react';
import { useState } from 'react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

/**
 * /admin/source-trust — §21.5 per-source trust scores viewer.
 *
 * Lists `silver.source_trust_scores` rows + feedback event counts.
 * Use to monitor how the source-trust training results vary across
 * the workspace and which sources have enough feedback to be
 * statistically meaningful.
 */

type Score = {
    trust_score_id: string;
    workspace_id: string;
    source_document_id: string;
    source_title: string | null;
    trust_score: number;
    model_version: string;
    computed_at: string;
    feedback_event_count: number;
};

type PageProps = {
    scores: Score[];
    fastapi_error: string | null;
    filter_workspace_id: string | null;
};

function badgeColour(score: number): string {
    if (score >= 0.8) return 'bg-green-100 text-green-800';
    if (score >= 0.6) return 'bg-amber-100 text-amber-800';
    if (score >= 0.4) return 'bg-orange-100 text-orange-800';
    return 'bg-red-100 text-red-800';
}

export default function SourceTrust({ scores, fastapi_error, filter_workspace_id }: PageProps): JSX.Element {
    // Phase 5 real-time push — train_source_trust broadcasts `source-trust`
    // on every successful training run (extended in Phase 5 alongside the
    // pre-existing `ml-training` dispatch from Phase 2).
    useAdminSurfaceUpdated('source-trust', null, () => {
        router.reload({ only: ['scores'] });
    });

    const [filter, setFilter] = useState<string>(filter_workspace_id ?? '');

    function applyFilter(): void {
        router.get('/admin/source-trust', filter ? { workspace_id: filter } : {}, {
            preserveScroll: true,
        });
    }

    return (
        <AppLayout>
            <Head title="Source Trust Scores" />
            <div className="px-6 py-4">
                <h1 className="text-2xl font-semibold mb-2">Source Trust Scores</h1>
                <p className="text-sm text-gray-600 mb-4">
                    §21.5 per-source trust. Updated by the
                    train_source_trust workflow; the deterministic
                    baseline blends citation validation rate (60%) +
                    recency decay (25%) + document-type prior (15%).
                </p>

                {fastapi_error && (
                    <div className="mb-3 p-3 bg-red-50 text-red-800 text-sm rounded">
                        Could not reach FastAPI: {fastapi_error}
                    </div>
                )}

                <div className="mb-4 flex gap-2 items-baseline">
                    <label className="text-sm">
                        Workspace filter:
                        <input
                            type="text"
                            value={filter}
                            onChange={e => setFilter(e.target.value)}
                            placeholder="(any) — UUID or blank"
                            className="ml-2 p-1.5 border rounded font-mono text-xs w-80"
                        />
                    </label>
                    <button
                        type="button"
                        onClick={applyFilter}
                        className="px-3 py-1.5 bg-blue-600 text-white rounded hover:bg-blue-700"
                    >
                        Apply
                    </button>
                </div>

                <table className="w-full text-sm border-collapse">
                    <thead>
                        <tr className="bg-gray-50 text-left">
                            <th className="py-2 px-2">Source</th>
                            <th className="py-2 px-2">Workspace</th>
                            <th className="py-2 px-2 text-right">Trust</th>
                            <th className="py-2 px-2 text-right">Feedback events</th>
                            <th className="py-2 px-2">Version</th>
                            <th className="py-2 px-2">Computed</th>
                        </tr>
                    </thead>
                    <tbody>
                        {scores.length === 0 && (
                            <tr>
                                <td colSpan={6} className="py-6 text-center text-gray-500">
                                    No source trust scores yet. Run the
                                    train_source_trust workflow from
                                    /admin/ml/training-runs.
                                </td>
                            </tr>
                        )}
                        {scores.map(s => (
                            <tr key={s.trust_score_id} className="border-b hover:bg-gray-50">
                                <td className="py-2 px-2">
                                    <div className="font-medium">{s.source_title ?? '—'}</div>
                                    <div className="text-xs text-gray-500 font-mono">
                                        {s.source_document_id.slice(0, 16)}…
                                    </div>
                                </td>
                                <td className="py-2 px-2 font-mono text-xs">
                                    {s.workspace_id.slice(0, 8)}…
                                </td>
                                <td className="py-2 px-2 text-right">
                                    <span className={`inline-block px-2 py-0.5 rounded text-sm font-mono ${badgeColour(s.trust_score)}`}>
                                        {s.trust_score.toFixed(3)}
                                    </span>
                                </td>
                                <td className="py-2 px-2 text-right">{s.feedback_event_count}</td>
                                <td className="py-2 px-2 text-xs font-mono text-gray-600">{s.model_version}</td>
                                <td className="py-2 px-2 text-xs text-gray-600">
                                    {new Date(s.computed_at).toLocaleString()}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </AppLayout>
    );
}
