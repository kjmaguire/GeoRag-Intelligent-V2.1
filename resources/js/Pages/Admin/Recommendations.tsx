import type { JSX } from 'react';
import { useState } from 'react';
import { Head } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';

/**
 * /admin/recommendations — §9.5 NBD + §9.6 Analogue Finder test-benches.
 * Two side-by-side panels; each invokes its respective agent and
 * shows the structured output.
 */

export default function Recommendations(): JSX.Element {
    const [workspaceId, setWorkspaceId] = useState<string>('a0000000-0000-0000-0000-000000000001');
    const [projectId, setProjectId] = useState<string>('');

    // NBD state
    const [nbdGaps, setNbdGaps] = useState<string>(
        'Conductive body suspected in the NE quadrant.\nAssay QAQC failures on PLS-22-08.\nStructural reinterpretation needed.',
    );
    const [nbdBudget, setNbdBudget] = useState<string>('');
    const [nbdResult, setNbdResult] = useState<Record<string, unknown> | null>(null);
    const [nbdBusy, setNbdBusy] = useState<boolean>(false);
    const [nbdError, setNbdError] = useState<string | null>(null);

    // Analogue state
    const [targetModel, setTargetModel] = useState<string>('athabasca_uranium');
    const [attrsJson, setAttrsJson] = useState<string>(JSON.stringify({
        deposit_model: 'unconformity_uranium',
        host_rocks: ['unconformity', 'basement_graphitic'],
        commodities: ['uranium'],
        tectonic_setting: 'athabasca',
    }, null, 2));
    const [anaResult, setAnaResult] = useState<Record<string, unknown> | null>(null);
    const [anaBusy, setAnaBusy] = useState<boolean>(false);
    const [anaError, setAnaError] = useState<string | null>(null);

    async function postJson(url: string, body: Record<string, unknown>): Promise<Response> {
        const csrf = (document.querySelector('meta[name="csrf-token"]') as HTMLMetaElement | null)?.content ?? '';
        return fetch(url, {
            method: 'POST',
            credentials: 'include',
            headers: {
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'X-CSRF-TOKEN': csrf,
            },
            body: JSON.stringify(body),
        });
    }

    async function runNbd(): Promise<void> {
        setNbdBusy(true);
        setNbdError(null);
        setNbdResult(null);
        try {
            const gaps = nbdGaps.split('\n').map(g => g.trim()).filter(Boolean);
            const body: Record<string, unknown> = {
                workspace_id: workspaceId,
                project_id: projectId || '00000000-0000-0000-0000-000000000000',
                evidence_gaps: gaps,
            };
            if (nbdBudget) body.budget_ceiling_usd = parseFloat(nbdBudget);
            const r = await postJson('/admin/recommendations/nbd', body);
            const j = await r.json();
            if (r.ok) setNbdResult(j); else setNbdError(j.error ?? 'NBD failed');
        } catch (e) {
            setNbdError(`Network: ${(e as Error).message}`);
        } finally {
            setNbdBusy(false);
        }
    }

    async function runAnalogue(): Promise<void> {
        setAnaBusy(true);
        setAnaError(null);
        setAnaResult(null);
        try {
            const attrs = JSON.parse(attrsJson);
            const r = await postJson('/admin/recommendations/analogue', {
                workspace_id: workspaceId,
                target_model_id: targetModel,
                project_attributes: attrs,
                top_k: 10,
            });
            const j = await r.json();
            if (r.ok) setAnaResult(j); else setAnaError(j.error ?? 'Analogue failed');
        } catch (e) {
            setAnaError(`Parse/network: ${(e as Error).message}`);
        } finally {
            setAnaBusy(false);
        }
    }

    return (
        <AppLayout>
            <Head title="Recommendations" />
            <div className="px-6 py-4">
                <h1 className="text-2xl font-semibold mb-2">Recommendations Test Bench</h1>
                <p className="text-sm text-gray-600 mb-4">
                    §9.5 Next-Best-Data + §9.6 Analogue Finder. Run each
                    agent against synthetic inputs to see what it would
                    recommend for an evidence gap or a deposit signature.
                </p>

                <div className="grid grid-cols-2 gap-2 mb-4">
                    <label className="text-sm">
                        Workspace
                        <input className="block w-full mt-1 p-1.5 border rounded font-mono text-xs"
                               value={workspaceId} onChange={e => setWorkspaceId(e.target.value)} />
                    </label>
                    <label className="text-sm">
                        Project (optional for NBD)
                        <input className="block w-full mt-1 p-1.5 border rounded font-mono text-xs"
                               value={projectId} onChange={e => setProjectId(e.target.value)} />
                    </label>
                </div>

                <div className="grid grid-cols-2 gap-4">
                    {/* NBD */}
                    <div className="p-3 border rounded bg-white">
                        <h2 className="text-lg font-semibold mb-2">Next-Best-Data (§9.5)</h2>
                        <label className="text-sm block">
                            Evidence gaps (one per line)
                            <textarea className="block w-full mt-1 p-1.5 border rounded text-sm" rows={5}
                                      value={nbdGaps} onChange={e => setNbdGaps(e.target.value)} />
                        </label>
                        <label className="text-sm block mt-2">
                            Budget ceiling USD (optional)
                            <input type="number" className="block w-full mt-1 p-1.5 border rounded"
                                   value={nbdBudget} onChange={e => setNbdBudget(e.target.value)} />
                        </label>
                        <button type="button" onClick={runNbd} disabled={nbdBusy}
                                className="mt-2 px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:bg-gray-300">
                            {nbdBusy ? 'Running…' : 'Run NBD'}
                        </button>
                        {nbdError && <div className="mt-2 p-2 bg-red-50 text-red-800 text-sm rounded">{nbdError}</div>}
                        {nbdResult && (
                            <pre className="mt-2 p-2 bg-gray-50 text-xs rounded max-h-72 overflow-auto">
                                {JSON.stringify(nbdResult, null, 2)}
                            </pre>
                        )}
                    </div>

                    {/* Analogue */}
                    <div className="p-3 border rounded bg-white">
                        <h2 className="text-lg font-semibold mb-2">Analogue Finder (§9.6)</h2>
                        <label className="text-sm block">
                            Target model id / slug
                            <input className="block w-full mt-1 p-1.5 border rounded font-mono text-xs"
                                   value={targetModel} onChange={e => setTargetModel(e.target.value)} />
                        </label>
                        <label className="text-sm block mt-2">
                            Project attributes (JSON)
                            <textarea className="block w-full mt-1 p-1.5 border rounded font-mono text-xs" rows={8}
                                      value={attrsJson} onChange={e => setAttrsJson(e.target.value)} />
                        </label>
                        <button type="button" onClick={runAnalogue} disabled={anaBusy}
                                className="mt-2 px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:bg-gray-300">
                            {anaBusy ? 'Running…' : 'Run Analogue'}
                        </button>
                        {anaError && <div className="mt-2 p-2 bg-red-50 text-red-800 text-sm rounded">{anaError}</div>}
                        {anaResult && (
                            <pre className="mt-2 p-2 bg-gray-50 text-xs rounded max-h-72 overflow-auto">
                                {JSON.stringify(anaResult, null, 2)}
                            </pre>
                        )}
                    </div>
                </div>
            </div>
        </AppLayout>
    );
}
