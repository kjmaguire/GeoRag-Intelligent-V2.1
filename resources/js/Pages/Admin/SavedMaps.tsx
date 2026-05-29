import type { JSX } from 'react';
import { useState } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { writeLayerVisibility } from '../../lib/layerVisibilityStorage';

type View = {
    view_id: string;
    workspace_id: string;
    project_id: string | null;
    name: string;
    payload: Record<string, unknown>;
    created_at: string;
};

type PageProps = { views: View[]; fastapi_error: string | null; filter_workspace_id: string | null };

export default function SavedMaps({ views, fastapi_error, filter_workspace_id }: PageProps): JSX.Element {
    const [filter, setFilter] = useState<string>(filter_workspace_id ?? '');
    const [expanded, setExpanded] = useState<string | null>(null);

    function restoreView(v: View): void {
        // view_state is intentionally unstructured (see SavedMapView model
        // docblock) but we recognise the conventional MapLibre camera keys:
        //   - center        [lon, lat]
        //   - zoom          number
        //   - pitch         number (degrees)
        //   - bearing       number (degrees)
        //   - bbox / bounds [minLon, minLat, maxLon, maxLat]
        //   - visibleLayers / layers  Record<string, boolean>
        // Missing keys are non-fatal — MapView keeps its current camera.
        const p: Record<string, unknown> = (v.payload ?? {}) as Record<string, unknown>;

        // 1. Apply layer-visibility prefs (if present) so MapView picks them
        //    up on its initial useState() from the same localStorage key.
        const layers = (p.visibleLayers ?? p.layers) as Record<string, boolean> | undefined;
        if (layers && typeof layers === 'object') {
            writeLayerVisibility(layers);
        }
        // 2. Stash the full payload for downstream restore (center/zoom/etc).
        try {
            window.sessionStorage.setItem(
                'georag:savedMapPayload',
                JSON.stringify({ view_id: v.view_id, project_id: v.project_id, payload: v.payload }),
            );
        } catch { /* private mode / quota — non-fatal */ }
        // 3. Navigate to /explorer; MapView listens for the dispatch below
        //    and applies center/zoom after the map is ready.
        router.visit('/explorer', {
            onFinish: () => {
                const center = (Array.isArray(p.center) && p.center.length === 2 ? p.center : undefined) as [number, number] | undefined;
                const zoom = typeof p.zoom === 'number' ? (p.zoom as number) : undefined;
                const pitch = typeof p.pitch === 'number' ? (p.pitch as number) : undefined;
                const bearing = typeof p.bearing === 'number' ? (p.bearing as number) : undefined;
                const bboxCandidate = (p.bbox ?? p.bounds) as [number, number, number, number] | undefined;
                const bbox = Array.isArray(bboxCandidate) && bboxCandidate.length === 4 ? bboxCandidate : undefined;
                window.dispatchEvent(new CustomEvent('georag:map:restore', {
                    detail: { center, zoom, pitch, bearing, bbox, view_id: v.view_id },
                }));
            },
        });
    }

    return (
        <AppLayout>
            <Head title="Saved Map Views" />
            <div className="px-6 py-4">
                <h1 className="text-2xl font-semibold mb-2">Saved Map Views</h1>
                <p className="text-sm text-gray-600 mb-4">
                    silver.saved_map_views — operator-saved map states
                    (centre, zoom, active layers). Useful for restoring
                    project context across sessions.
                </p>

                {fastapi_error && <div className="mb-3 p-3 bg-red-50 text-red-800 text-sm rounded">{fastapi_error}</div>}

                <div className="mb-3 flex gap-2 items-baseline">
                    <input className="p-1.5 border rounded font-mono text-xs w-80" placeholder="Filter workspace UUID"
                           value={filter} onChange={e => setFilter(e.target.value)} />
                    <button type="button" onClick={() => router.get('/admin/saved-maps', filter ? { workspace_id: filter } : {})}
                            className="px-3 py-1.5 bg-blue-600 text-white rounded hover:bg-blue-700">Apply</button>
                </div>

                <table className="w-full text-sm border-collapse">
                    <thead>
                        <tr className="bg-gray-50 text-left">
                            <th className="py-2 px-2">Name</th>
                            <th className="py-2 px-2">Workspace</th>
                            <th className="py-2 px-2">Project</th>
                            <th className="py-2 px-2">Created</th>
                            <th className="py-2 px-2 text-right">Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        {views.length === 0 && <tr><td colSpan={5} className="py-6 text-center text-gray-500">No saved map views.</td></tr>}
                        {views.map(v => (
                            <>
                                <tr key={v.view_id} className="border-b hover:bg-gray-50 cursor-pointer"
                                    onClick={() => setExpanded(expanded === v.view_id ? null : v.view_id)}>
                                    <td className="py-2 px-2">{v.name}</td>
                                    <td className="py-2 px-2 font-mono text-xs">{v.workspace_id.slice(0, 8)}…</td>
                                    <td className="py-2 px-2 font-mono text-xs">
                                        {v.project_id ? v.project_id.slice(0, 8) + '…' : '—'}
                                    </td>
                                    <td className="py-2 px-2 text-xs text-gray-600">{new Date(v.created_at).toLocaleString()}</td>
                                    <td className="py-2 px-2 text-right">
                                        <button
                                            type="button"
                                            onClick={e => { e.stopPropagation(); restoreView(v); }}
                                            className="px-2 py-1 bg-blue-600 text-white text-xs rounded hover:bg-blue-700"
                                            title="Apply this view's center/zoom/layers in /explorer"
                                        >
                                            Restore
                                        </button>
                                    </td>
                                </tr>
                                {expanded === v.view_id && (
                                    <tr key={v.view_id + '-detail'} className="bg-gray-50">
                                        <td colSpan={5} className="py-2 px-4">
                                            <pre className="text-xs whitespace-pre-wrap max-h-72 overflow-auto">
                                                {JSON.stringify(v.payload, null, 2)}
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
