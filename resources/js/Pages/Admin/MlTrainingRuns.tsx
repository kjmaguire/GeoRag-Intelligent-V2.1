import type { JSX } from 'react';
import { useState } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

/**
 * /admin/ml/training-runs — Phase H4 §12 UI.
 *
 * Lists recent target-model + source-trust training runs from the
 * audit ledger, plus a trigger form for each workflow.
 */

type TrainingRun = {
    run_id: string;
    workspace_id: string | null;
    kind: string;          // 'target_model' | 'source_trust'
    actor_id: number | null;
    completed_at: string;
    success: boolean;
    metrics: Record<string, unknown>;
};

type PageProps = {
    runs: TrainingRun[];
    fastapi_error: string | null;
};

export default function MlTrainingRuns({ runs, fastapi_error }: PageProps): JSX.Element {
    // Phase 2 real-time push — train_target_model / train_source_trust both
    // broadcast to admin.ml-training on completion (success or failure).
    useAdminSurfaceUpdated('ml-training', null, () => {
        router.reload({ only: ['runs'] });
    });

    const [targetModelId, setTargetModelId] = useState<string>('');
    const [activate, setActivate] = useState<boolean>(false);
    const [workspaceId, setWorkspaceId] = useState<string>('');
    const [modelVersion, setModelVersion] = useState<string>('weighted_learned_v1');
    const [busy, setBusy] = useState<string | null>(null);
    const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);

    async function fireTrain(endpoint: 'train-target-model' | 'train-source-trust', payload: Record<string, unknown>): Promise<void> {
        setBusy(endpoint);
        setResult(null);
        try {
            const csrf = (document.querySelector('meta[name="csrf-token"]') as HTMLMetaElement | null)?.content ?? '';
            const resp = await fetch(`/admin/ml/${endpoint}`, {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'X-CSRF-TOKEN': csrf,
                },
                body: JSON.stringify(payload),
            });
            const body = await resp.json();
            if (resp.ok) {
                setResult({
                    ok: true,
                    message: `Training complete — ${endpoint}: ${
                        body.new_version_id ? `version_id=${body.new_version_id.slice(0, 8)}…` :
                        body.sources_scored != null ? `${body.sources_scored} source(s) scored` :
                        'done'
                    }`,
                });
                router.reload({ only: ['runs'] });
            } else {
                setResult({ ok: false, message: body.error ?? 'Training failed.' });
            }
        } catch (err) {
            setResult({ ok: false, message: `Network error: ${(err as Error).message}` });
        } finally {
            setBusy(null);
        }
    }

    return (
        <AppLayout>
            <Head title="ML Training Runs" />
            <div className="px-6 py-4">
                <h1 className="text-2xl font-semibold mb-2">ML Training Runs</h1>
                <p className="text-sm text-gray-600 mb-4">
                    §12.3 target-model + §12.7 source-trust training.
                    Phase H4 runs deterministic baselines that do not
                    require xgboost; the same workflows pick up xgboost
                    automatically when the dep ships.
                </p>

                {fastapi_error && (
                    <div className="mb-3 p-3 bg-red-50 text-red-800 text-sm rounded">
                        Could not reach FastAPI: {fastapi_error}
                    </div>
                )}

                <div className="grid grid-cols-2 gap-4 mb-6">
                    <div className="p-3 border rounded bg-white">
                        <h2 className="text-lg font-semibold mb-2">Train target model</h2>
                        <label className="text-sm block">
                            Target model id (UUID)
                            <input
                                type="text"
                                value={targetModelId}
                                onChange={e => setTargetModelId(e.target.value)}
                                className="block w-full mt-1 p-1.5 border rounded font-mono text-xs"
                            />
                        </label>
                        <label className="text-sm block mt-3">
                            <input
                                type="checkbox"
                                checked={activate}
                                onChange={e => setActivate(e.target.checked)}
                                className="mr-2"
                            />
                            Activate new version (deactivates others)
                        </label>
                        <button
                            type="button"
                            onClick={() => fireTrain('train-target-model', {
                                target_model_id: targetModelId,
                                initiated_by_user_id: 1,
                                activate_on_success: activate,
                            })}
                            disabled={busy !== null || !targetModelId}
                            className="mt-3 px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:bg-gray-300"
                        >
                            {busy === 'train-target-model' ? 'Training…' : 'Train'}
                        </button>
                    </div>

                    <div className="p-3 border rounded bg-white">
                        <h2 className="text-lg font-semibold mb-2">Train source trust</h2>
                        <label className="text-sm block">
                            Workspace id (UUID)
                            <input
                                type="text"
                                value={workspaceId}
                                onChange={e => setWorkspaceId(e.target.value)}
                                className="block w-full mt-1 p-1.5 border rounded font-mono text-xs"
                            />
                        </label>
                        <label className="text-sm block mt-3">
                            Model version tag
                            <input
                                type="text"
                                value={modelVersion}
                                onChange={e => setModelVersion(e.target.value)}
                                className="block w-full mt-1 p-1.5 border rounded font-mono text-xs"
                            />
                        </label>
                        <button
                            type="button"
                            onClick={() => fireTrain('train-source-trust', {
                                workspace_id: workspaceId,
                                initiated_by_user_id: 1,
                                model_version: modelVersion,
                            })}
                            disabled={busy !== null || !workspaceId}
                            className="mt-3 px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:bg-gray-300"
                        >
                            {busy === 'train-source-trust' ? 'Training…' : 'Train'}
                        </button>
                    </div>
                </div>

                {result && (
                    <div className={`mb-4 p-3 rounded text-sm ${
                        result.ok ? 'bg-green-50 text-green-800' : 'bg-red-50 text-red-800'
                    }`}>
                        {result.message}
                    </div>
                )}

                <h2 className="text-lg font-semibold mb-2">Recent runs</h2>
                <table className="w-full text-sm border-collapse">
                    <thead>
                        <tr className="bg-gray-50 text-left">
                            <th className="py-2 px-2">Kind</th>
                            <th className="py-2 px-2">Workspace</th>
                            <th className="py-2 px-2">Actor</th>
                            <th className="py-2 px-2">Method</th>
                            <th className="py-2 px-2">Completed</th>
                        </tr>
                    </thead>
                    <tbody>
                        {runs.length === 0 && (
                            <tr>
                                <td colSpan={5} className="py-4 text-center text-gray-500">
                                    No training runs recorded yet.
                                </td>
                            </tr>
                        )}
                        {runs.map(r => (
                            <tr key={r.run_id} className="border-b hover:bg-gray-50">
                                <td className="py-2 px-2">
                                    <span className={`inline-block px-2 py-0.5 rounded text-xs ${
                                        r.kind === 'target_model'
                                            ? 'bg-blue-100 text-blue-800'
                                            : 'bg-amber-100 text-amber-800'
                                    }`}>
                                        {r.kind}
                                    </span>
                                </td>
                                <td className="py-2 px-2 font-mono text-xs">
                                    {r.workspace_id ? r.workspace_id.slice(0, 8) + '…' : 'global'}
                                </td>
                                <td className="py-2 px-2">{r.actor_id ?? 'system'}</td>
                                <td className="py-2 px-2 text-xs text-gray-600">
                                    {String((r.metrics as Record<string, unknown>).method ?? '—')}
                                </td>
                                <td className="py-2 px-2 text-xs text-gray-600">
                                    {new Date(r.completed_at).toLocaleString()}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </AppLayout>
    );
}
