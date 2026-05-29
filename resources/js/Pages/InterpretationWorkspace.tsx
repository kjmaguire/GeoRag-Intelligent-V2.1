import type { JSX } from 'react';
import { useEffect, useRef, useState, useCallback } from 'react';
import { Head, Link } from '@inertiajs/react';
import maplibregl, { type Map as MlMap, type LngLat } from 'maplibre-gl';
import AppLayout from '../Layouts/AppLayout';

/**
 * /projects/{projectId}/interpretation — §19.3 Interpretation Workspace.
 *
 * MapLibre canvas + three drawing modes:
 *   - "note"     click to drop a point + open inline note editor
 *   - "section"  click multiple points → LineString cross-section trace
 *   - "zone"     click multiple points → Polygon target zone (auto-closes on dbl-click)
 *
 * All artifacts persist via /api/v1/interpretation/{notes,section-lines,target-zones}.
 */

interface PageProps {
    project_id: string;
    workspace_id: string;
}

interface Note {
    note_id: string;
    title: string | null;
    body_md: string;
    anchor_geojson: { type: string; coordinates: [number, number] } | null;
    tags: string[];
    created_at: string;
}

interface SectionLine {
    section_id: string;
    name: string | null;
    azimuth_deg: number | null;
    geojson: { type: string; coordinates: [number, number][] };
    notes: string | null;
    created_at: string;
}

interface TargetZone {
    zone_id: string;
    name: string;
    rationale: string | null;
    commodity: string | null;
    confidence: string;
    geojson: { type: string; coordinates: [number, number][][] };
    accepted: boolean;
    created_at: string;
}

type DrawMode = 'none' | 'note' | 'section' | 'zone';

function csrfToken(): string {
    return (document.querySelector('meta[name="csrf-token"]') as HTMLMetaElement)?.content ?? '';
}

async function apiCall(path: string, init?: RequestInit): Promise<Response> {
    return fetch(path, {
        credentials: 'same-origin',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRF-TOKEN': csrfToken(),
            Accept: 'application/json',
            ...(init?.headers ?? {}),
        },
        ...init,
    });
}

export default function InterpretationWorkspace({ project_id, workspace_id }: PageProps): JSX.Element {
    const mapContainer = useRef<HTMLDivElement | null>(null);
    const mapRef = useRef<MlMap | null>(null);
    const [drawMode, setDrawMode] = useState<DrawMode>('none');
    const drawModeRef = useRef<DrawMode>('none');
    const pendingPointsRef = useRef<[number, number][]>([]);

    const [notes, setNotes] = useState<Note[]>([]);
    const [sections, setSections] = useState<SectionLine[]>([]);
    const [zones, setZones] = useState<TargetZone[]>([]);

    const [noteDraft, setNoteDraft] = useState<{ open: boolean; lngLat: LngLat | null; body: string; title: string; tags: string }>({
        open: false, lngLat: null, body: '', title: '', tags: '',
    });
    const [zoneDraft, setZoneDraft] = useState<{ open: boolean; coords: [number, number][]; name: string; commodity: string; confidence: string; rationale: string }>({
        open: false, coords: [], name: '', commodity: '', confidence: 'medium', rationale: '',
    });
    const [sectionDraft, setSectionDraft] = useState<{ open: boolean; coords: [number, number][]; name: string; notes: string }>({
        open: false, coords: [], name: '', notes: '',
    });

    // Sync draw mode ref for closures
    useEffect(() => { drawModeRef.current = drawMode; }, [drawMode]);

    // ── Initial data load ──────────────────────────────────────────
    const loadAll = useCallback(async () => {
        const [n, s, z] = await Promise.all([
            apiCall(`/api/v1/interpretation/notes?project_id=${project_id}`),
            apiCall(`/api/v1/interpretation/section-lines?project_id=${project_id}`),
            apiCall(`/api/v1/interpretation/target-zones?project_id=${project_id}`),
        ]);
        if (n.ok) setNotes(await n.json());
        if (s.ok) setSections(await s.json());
        if (z.ok) setZones(await z.json());
    }, [project_id]);

    useEffect(() => { loadAll(); }, [loadAll]);

    // ── Map init ────────────────────────────────────────────────────
    useEffect(() => {
        if (!mapContainer.current || mapRef.current) return;

        const map = new maplibregl.Map({
            container: mapContainer.current,
            style: {
                version: 8,
                sources: {
                    osm: {
                        type: 'raster',
                        tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
                        tileSize: 256,
                        attribution: '© OpenStreetMap',
                    },
                },
                layers: [{ id: 'osm', type: 'raster', source: 'osm' }],
            },
            center: [-106, 56],
            zoom: 5,
        });
        mapRef.current = map;

        map.on('load', () => {
            // Layers for existing artifacts
            map.addSource('notes', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
            map.addLayer({
                id: 'notes-pts', type: 'circle', source: 'notes',
                paint: { 'circle-radius': 6, 'circle-color': '#facc15', 'circle-stroke-width': 1, 'circle-stroke-color': '#854d0e' },
            });
            map.addSource('sections', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
            map.addLayer({
                id: 'sections-lines', type: 'line', source: 'sections',
                paint: { 'line-color': '#22c55e', 'line-width': 3 },
            });
            map.addSource('zones', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
            map.addLayer({
                id: 'zones-fill', type: 'fill', source: 'zones',
                paint: { 'fill-color': '#a78bfa', 'fill-opacity': 0.35 },
            });
            map.addLayer({
                id: 'zones-stroke', type: 'line', source: 'zones',
                paint: { 'line-color': '#7c3aed', 'line-width': 2 },
            });
            // Pending draw preview
            map.addSource('pending', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
            map.addLayer({
                id: 'pending-pts', type: 'circle', source: 'pending',
                paint: { 'circle-radius': 4, 'circle-color': '#ef4444' },
                filter: ['==', ['geometry-type'], 'Point'],
            });
            map.addLayer({
                id: 'pending-line', type: 'line', source: 'pending',
                paint: { 'line-color': '#ef4444', 'line-width': 2, 'line-dasharray': [2, 2] },
                filter: ['==', ['geometry-type'], 'LineString'],
            });
            map.addLayer({
                id: 'pending-fill', type: 'fill', source: 'pending',
                paint: { 'fill-color': '#ef4444', 'fill-opacity': 0.15 },
                filter: ['==', ['geometry-type'], 'Polygon'],
            });
        });

        // Click handler — depends on current draw mode
        map.on('click', (e) => {
            const mode = drawModeRef.current;
            if (mode === 'none') return;
            const lng = e.lngLat.lng;
            const lat = e.lngLat.lat;

            if (mode === 'note') {
                setNoteDraft({ open: true, lngLat: e.lngLat, body: '', title: '', tags: '' });
                setDrawMode('none');
            } else if (mode === 'section' || mode === 'zone') {
                pendingPointsRef.current.push([lng, lat]);
                redrawPending();
            }
        });
        // Double-click closes section/zone
        map.on('dblclick', (e) => {
            e.preventDefault();
            const mode = drawModeRef.current;
            if (mode === 'section' && pendingPointsRef.current.length >= 2) {
                setSectionDraft({ open: true, coords: [...pendingPointsRef.current], name: '', notes: '' });
                pendingPointsRef.current = [];
                setDrawMode('none');
                redrawPending();
            } else if (mode === 'zone' && pendingPointsRef.current.length >= 3) {
                setZoneDraft({
                    open: true,
                    coords: [...pendingPointsRef.current],
                    name: '', commodity: '', confidence: 'medium', rationale: '',
                });
                pendingPointsRef.current = [];
                setDrawMode('none');
                redrawPending();
            }
        });

        return () => {
            map.remove();
            mapRef.current = null;
        };
    }, []);

    // ── Update sources whenever artifacts change ────────────────────
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !map.isStyleLoaded()) return;
        const src = map.getSource('notes') as maplibregl.GeoJSONSource | undefined;
        if (src) {
            src.setData({
                type: 'FeatureCollection',
                features: notes
                    .filter((n) => n.anchor_geojson)
                    .map((n) => ({
                        type: 'Feature',
                        geometry: n.anchor_geojson as GeoJSON.Geometry,
                        properties: { note_id: n.note_id, title: n.title || '(untitled)' },
                    })),
            });
        }
    }, [notes]);

    useEffect(() => {
        const map = mapRef.current;
        if (!map || !map.isStyleLoaded()) return;
        const src = map.getSource('sections') as maplibregl.GeoJSONSource | undefined;
        if (src) {
            src.setData({
                type: 'FeatureCollection',
                features: sections.map((s) => ({
                    type: 'Feature',
                    geometry: s.geojson as GeoJSON.Geometry,
                    properties: { section_id: s.section_id, name: s.name || '(unnamed)' },
                })),
            });
        }
    }, [sections]);

    useEffect(() => {
        const map = mapRef.current;
        if (!map || !map.isStyleLoaded()) return;
        const src = map.getSource('zones') as maplibregl.GeoJSONSource | undefined;
        if (src) {
            src.setData({
                type: 'FeatureCollection',
                features: zones.map((z) => ({
                    type: 'Feature',
                    geometry: z.geojson as GeoJSON.Geometry,
                    properties: { zone_id: z.zone_id, name: z.name, accepted: z.accepted },
                })),
            });
        }
    }, [zones]);

    function redrawPending() {
        const map = mapRef.current;
        const src = map?.getSource('pending') as maplibregl.GeoJSONSource | undefined;
        if (!src) return;
        const pts = pendingPointsRef.current;
        const features: GeoJSON.Feature[] = pts.map((c) => ({
            type: 'Feature',
            geometry: { type: 'Point', coordinates: c },
            properties: {},
        }));
        if (pts.length >= 2) {
            features.push({
                type: 'Feature',
                geometry: { type: 'LineString', coordinates: pts },
                properties: {},
            });
        }
        if (drawModeRef.current === 'zone' && pts.length >= 3) {
            const closed = [...pts, pts[0]];
            features.push({
                type: 'Feature',
                geometry: { type: 'Polygon', coordinates: [closed] },
                properties: {},
            });
        }
        src.setData({ type: 'FeatureCollection', features });
    }

    function startMode(mode: DrawMode) {
        pendingPointsRef.current = [];
        redrawPending();
        setDrawMode(mode);
    }

    function cancelDraw() {
        pendingPointsRef.current = [];
        redrawPending();
        setDrawMode('none');
        setNoteDraft({ open: false, lngLat: null, body: '', title: '', tags: '' });
        setSectionDraft({ open: false, coords: [], name: '', notes: '' });
        setZoneDraft({ open: false, coords: [], name: '', commodity: '', confidence: 'medium', rationale: '' });
    }

    // ── Submit handlers ─────────────────────────────────────────────
    async function submitNote() {
        if (!noteDraft.lngLat || !noteDraft.body.trim()) return;
        const r = await apiCall('/api/v1/interpretation/notes', {
            method: 'POST',
            body: JSON.stringify({
                project_id,
                title: noteDraft.title || null,
                body_md: noteDraft.body,
                anchor_geojson: { type: 'Point', coordinates: [noteDraft.lngLat.lng, noteDraft.lngLat.lat] },
                tags: noteDraft.tags.split(',').map((t) => t.trim()).filter(Boolean),
            }),
        });
        if (r.ok) {
            const created: Note = await r.json();
            setNotes((prev) => [created, ...prev]);
            cancelDraw();
        } else {
            alert('Failed to save note');
        }
    }

    async function submitSection() {
        if (sectionDraft.coords.length < 2) return;
        const r = await apiCall('/api/v1/interpretation/section-lines', {
            method: 'POST',
            body: JSON.stringify({
                project_id,
                name: sectionDraft.name || null,
                geojson: { type: 'LineString', coordinates: sectionDraft.coords },
                notes: sectionDraft.notes || null,
            }),
        });
        if (r.ok) {
            const created: SectionLine = await r.json();
            setSections((prev) => [created, ...prev]);
            cancelDraw();
        } else {
            alert('Failed to save section');
        }
    }

    async function submitZone() {
        if (zoneDraft.coords.length < 3 || !zoneDraft.name) return;
        const closed = [...zoneDraft.coords, zoneDraft.coords[0]];
        const r = await apiCall('/api/v1/interpretation/target-zones', {
            method: 'POST',
            body: JSON.stringify({
                project_id,
                name: zoneDraft.name,
                rationale: zoneDraft.rationale || null,
                commodity: zoneDraft.commodity || null,
                confidence: zoneDraft.confidence,
                geojson: { type: 'Polygon', coordinates: [closed] },
            }),
        });
        if (r.ok) {
            const created: TargetZone = await r.json();
            setZones((prev) => [created, ...prev]);
            cancelDraw();
        } else {
            alert('Failed to save zone');
        }
    }

    async function acceptZone(zoneId: string) {
        const r = await apiCall(`/api/v1/interpretation/target-zones/${zoneId}/accept`, { method: 'POST' });
        if (r.ok) {
            const updated: TargetZone = await r.json();
            setZones((prev) => prev.map((z) => (z.zone_id === zoneId ? updated : z)));
        }
    }

    async function deleteArtifact(kind: 'notes' | 'section-lines' | 'target-zones', id: string) {
        if (!confirm('Delete this item?')) return;
        const r = await apiCall(`/api/v1/interpretation/${kind}/${id}`, { method: 'DELETE' });
        if (r.ok || r.status === 204) {
            if (kind === 'notes') setNotes((p) => p.filter((n) => n.note_id !== id));
            else if (kind === 'section-lines') setSections((p) => p.filter((s) => s.section_id !== id));
            else setZones((p) => p.filter((z) => z.zone_id !== id));
        }
    }

    const modeBadge = (
        <span className={`ml-2 inline-flex items-center rounded px-2 py-0.5 text-xs ${drawMode === 'none' ? 'bg-zinc-200 text-zinc-600' : 'bg-red-100 text-red-700'}`}>
            {drawMode === 'none' ? 'View mode' :
             drawMode === 'note' ? 'Click to drop note pin' :
             drawMode === 'section' ? 'Click points; double-click to finish line' :
             'Click points; double-click to close polygon'}
        </span>
    );

    return (
        <AppLayout>
            <Head title="Interpretation Workspace" />

            <div className="flex h-[calc(100vh-4rem)] flex-col">
                {/* Top bar */}
                <div className="border-b border-zinc-200 bg-white px-4 py-2">
                    <div className="flex items-center gap-3">
                        <Link href={`/projects/${project_id}`} className="text-sm text-indigo-600 hover:underline">
                            ← Project
                        </Link>
                        <h1 className="text-lg font-semibold text-zinc-900">Interpretation Workspace</h1>
                        {modeBadge}
                        <div className="ml-auto flex gap-2">
                            <button
                                type="button"
                                onClick={() => startMode('note')}
                                className={`rounded px-3 py-1 text-sm ${drawMode === 'note' ? 'bg-amber-500 text-white' : 'bg-amber-100 text-amber-800 hover:bg-amber-200'}`}
                            >
                                + Note
                            </button>
                            <button
                                type="button"
                                onClick={() => startMode('section')}
                                className={`rounded px-3 py-1 text-sm ${drawMode === 'section' ? 'bg-emerald-600 text-white' : 'bg-emerald-100 text-emerald-800 hover:bg-emerald-200'}`}
                            >
                                + Section line
                            </button>
                            <button
                                type="button"
                                onClick={() => startMode('zone')}
                                className={`rounded px-3 py-1 text-sm ${drawMode === 'zone' ? 'bg-violet-600 text-white' : 'bg-violet-100 text-violet-800 hover:bg-violet-200'}`}
                            >
                                + Target zone
                            </button>
                            {drawMode !== 'none' && (
                                <button type="button" onClick={cancelDraw} className="rounded bg-zinc-200 px-3 py-1 text-sm">
                                    Cancel
                                </button>
                            )}
                        </div>
                    </div>
                </div>

                <div className="flex flex-1 overflow-hidden">
                    {/* Map */}
                    <div ref={mapContainer} className="flex-1" />

                    {/* Right sidebar — artifact list */}
                    <aside className="w-80 overflow-y-auto border-l border-zinc-200 bg-zinc-50 p-3">
                        <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
                            Notes ({notes.length})
                        </h2>
                        <ul className="mb-4 mt-2 space-y-1">
                            {notes.map((n) => (
                                <li key={n.note_id} className="rounded border border-zinc-200 bg-white p-2 text-xs">
                                    <div className="flex items-center justify-between">
                                        <span className="font-medium">{n.title || '(untitled)'}</span>
                                        <button onClick={() => deleteArtifact('notes', n.note_id)} className="text-red-500 hover:text-red-700">✕</button>
                                    </div>
                                    <div className="mt-1 line-clamp-2 text-zinc-600">{n.body_md}</div>
                                    {n.tags.length > 0 && (
                                        <div className="mt-1 flex flex-wrap gap-1">
                                            {n.tags.map((t) => <span key={t} className="rounded bg-zinc-100 px-1.5 py-0.5 text-[10px]">{t}</span>)}
                                        </div>
                                    )}
                                </li>
                            ))}
                        </ul>

                        <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
                            Section lines ({sections.length})
                        </h2>
                        <ul className="mb-4 mt-2 space-y-1">
                            {sections.map((s) => (
                                <li key={s.section_id} className="rounded border border-zinc-200 bg-white p-2 text-xs">
                                    <div className="flex items-center justify-between">
                                        <span className="font-medium">{s.name || '(unnamed)'}</span>
                                        <button onClick={() => deleteArtifact('section-lines', s.section_id)} className="text-red-500 hover:text-red-700">✕</button>
                                    </div>
                                    <div className="mt-1 text-zinc-500">{s.geojson.coordinates.length} points</div>
                                </li>
                            ))}
                        </ul>

                        <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
                            Target zones ({zones.length})
                        </h2>
                        <ul className="mt-2 space-y-1">
                            {zones.map((z) => (
                                <li key={z.zone_id} className="rounded border border-zinc-200 bg-white p-2 text-xs">
                                    <div className="flex items-center justify-between">
                                        <span className="font-medium">{z.name}</span>
                                        <button onClick={() => deleteArtifact('target-zones', z.zone_id)} className="text-red-500 hover:text-red-700">✕</button>
                                    </div>
                                    <div className="mt-1 flex items-center gap-2 text-zinc-600">
                                        {z.commodity && <span>{z.commodity}</span>}
                                        <span className={
                                            z.confidence === 'high' ? 'text-emerald-700' :
                                            z.confidence === 'low'  ? 'text-red-700'      :
                                                                      'text-amber-700'
                                        }>· {z.confidence}</span>
                                        {z.accepted ? (
                                            <span className="ml-auto rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] text-emerald-800">accepted</span>
                                        ) : (
                                            <button onClick={() => acceptZone(z.zone_id)} className="ml-auto text-[10px] text-indigo-600 hover:underline">accept</button>
                                        )}
                                    </div>
                                </li>
                            ))}
                        </ul>
                    </aside>
                </div>

                {/* Note draft modal */}
                {noteDraft.open && (
                    <div className="absolute inset-0 z-50 flex items-center justify-center bg-zinc-900/40">
                        <div className="w-96 rounded-lg bg-white p-4 shadow-xl">
                            <h3 className="text-base font-semibold">New note</h3>
                            <input
                                placeholder="Title (optional)"
                                value={noteDraft.title}
                                onChange={(e) => setNoteDraft({ ...noteDraft, title: e.target.value })}
                                className="mt-3 block w-full rounded-md border-zinc-300 text-sm"
                            />
                            <textarea
                                rows={5}
                                placeholder="Note body (markdown supported)"
                                value={noteDraft.body}
                                onChange={(e) => setNoteDraft({ ...noteDraft, body: e.target.value })}
                                className="mt-2 block w-full rounded-md border-zinc-300 text-sm"
                                autoFocus
                            />
                            <input
                                placeholder="Tags (comma-separated)"
                                value={noteDraft.tags}
                                onChange={(e) => setNoteDraft({ ...noteDraft, tags: e.target.value })}
                                className="mt-2 block w-full rounded-md border-zinc-300 text-sm"
                            />
                            <div className="mt-3 flex justify-end gap-2">
                                <button onClick={cancelDraw} className="rounded border border-zinc-300 px-3 py-1 text-sm">Cancel</button>
                                <button onClick={submitNote} className="rounded bg-indigo-600 px-3 py-1 text-sm text-white hover:bg-indigo-500">Save</button>
                            </div>
                        </div>
                    </div>
                )}

                {/* Section draft modal */}
                {sectionDraft.open && (
                    <div className="absolute inset-0 z-50 flex items-center justify-center bg-zinc-900/40">
                        <div className="w-96 rounded-lg bg-white p-4 shadow-xl">
                            <h3 className="text-base font-semibold">New section line</h3>
                            <p className="mt-1 text-xs text-zinc-500">{sectionDraft.coords.length} points</p>
                            <input
                                placeholder="Section name (optional)"
                                value={sectionDraft.name}
                                onChange={(e) => setSectionDraft({ ...sectionDraft, name: e.target.value })}
                                className="mt-3 block w-full rounded-md border-zinc-300 text-sm"
                                autoFocus
                            />
                            <textarea
                                rows={3}
                                placeholder="Notes (optional)"
                                value={sectionDraft.notes}
                                onChange={(e) => setSectionDraft({ ...sectionDraft, notes: e.target.value })}
                                className="mt-2 block w-full rounded-md border-zinc-300 text-sm"
                            />
                            <div className="mt-3 flex justify-end gap-2">
                                <button onClick={cancelDraw} className="rounded border border-zinc-300 px-3 py-1 text-sm">Cancel</button>
                                <button onClick={submitSection} className="rounded bg-emerald-600 px-3 py-1 text-sm text-white hover:bg-emerald-500">Save</button>
                            </div>
                        </div>
                    </div>
                )}

                {/* Zone draft modal */}
                {zoneDraft.open && (
                    <div className="absolute inset-0 z-50 flex items-center justify-center bg-zinc-900/40">
                        <div className="w-96 rounded-lg bg-white p-4 shadow-xl">
                            <h3 className="text-base font-semibold">New target zone</h3>
                            <p className="mt-1 text-xs text-zinc-500">{zoneDraft.coords.length} vertices</p>
                            <input
                                required
                                placeholder="Zone name *"
                                value={zoneDraft.name}
                                onChange={(e) => setZoneDraft({ ...zoneDraft, name: e.target.value })}
                                className="mt-3 block w-full rounded-md border-zinc-300 text-sm"
                                autoFocus
                            />
                            <input
                                placeholder="Commodity (e.g. uranium, gold)"
                                value={zoneDraft.commodity}
                                onChange={(e) => setZoneDraft({ ...zoneDraft, commodity: e.target.value })}
                                className="mt-2 block w-full rounded-md border-zinc-300 text-sm"
                            />
                            <select
                                value={zoneDraft.confidence}
                                onChange={(e) => setZoneDraft({ ...zoneDraft, confidence: e.target.value })}
                                className="mt-2 block w-full rounded-md border-zinc-300 text-sm"
                            >
                                <option value="low">Low confidence</option>
                                <option value="medium">Medium confidence</option>
                                <option value="high">High confidence</option>
                            </select>
                            <textarea
                                rows={3}
                                placeholder="Rationale (why is this a target?)"
                                value={zoneDraft.rationale}
                                onChange={(e) => setZoneDraft({ ...zoneDraft, rationale: e.target.value })}
                                className="mt-2 block w-full rounded-md border-zinc-300 text-sm"
                            />
                            <div className="mt-3 flex justify-end gap-2">
                                <button onClick={cancelDraw} className="rounded border border-zinc-300 px-3 py-1 text-sm">Cancel</button>
                                <button onClick={submitZone} className="rounded bg-violet-600 px-3 py-1 text-sm text-white hover:bg-violet-500">Save</button>
                            </div>
                        </div>
                    </div>
                )}
            </div>
        </AppLayout>
    );
}
