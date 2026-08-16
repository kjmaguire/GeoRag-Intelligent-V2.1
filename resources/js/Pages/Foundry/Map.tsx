import { lazy, Suspense } from 'react';
import { Head, Link } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, EmptyState } from '@/Components/Foundry/primitives';

// MapView pulls in maplibre-gl (WebGL, ~200kb) — lazy-load it the same way
// InlineViz.tsx does so the Map page's own bundle stays thin and every
// other Foundry page's initial load is unaffected.
const MapView = lazy(() => import('@/Components/MapView'));

interface MapProjectProps {
    project_id: string;
    project_name: string;
    slug: string;
    crs_epsg: number | null;
}

interface MapPageProps {
    project: MapProjectProps;
    collar_count: number;
}

function LoadingPanel() {
    return (
        <div className="flex-1 flex items-center justify-center" style={{ background: 'var(--bg-0)' }}>
            <div className="flex items-center gap-2 text-xs" style={{ color: 'var(--fg-3)' }}>
                <div className="w-4 h-4 rounded-full border-2 animate-spin" style={{ borderColor: 'var(--line-2)', borderTopColor: 'var(--accent)' }} />
                Loading map…
            </div>
        </div>
    );
}

export default function FoundryMap({ project, collar_count }: MapPageProps) {
    const hasCollars = collar_count > 0;

    return (
        <AppLayout>
            <Head title={`${project.project_name} — Map`} />

            <div className="flex-1 flex flex-col overflow-hidden" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow="PROJECT · MAP"
                    title="Map"
                    sub={
                        <span>
                            {collar_count} drill collar{collar_count === 1 ? '' : 's'}
                            {project.crs_epsg && <span> · EPSG:{project.crs_epsg}</span>}
                        </span>
                    }
                />

                {hasCollars ? (
                    <div className="flex-1 relative">
                        <Suspense fallback={<LoadingPanel />}>
                            {/* useMartinTiles is pinned false — the Martin tile
                                server + its /tiles/* Laravel proxy were both
                                removed in the frontend trim, so the MVT branch
                                is infrastructure-dead. MapView self-fetches real
                                collar GeoJSON via GET
                                /api/v1/projects/{project_id}/collars (the same
                                query CollarController::index already runs) —
                                no inlineGeoJson here, that prop is reserved for
                                the chat-derived payload InlineViz renders. */}
                            <MapView
                                projectId={project.project_id}
                                useMartinTiles={false}
                                crs={project.crs_epsg ? `EPSG:${project.crs_epsg}` : undefined}
                            />
                        </Suspense>
                    </div>
                ) : (
                    <section className="px-8 py-8">
                        <EmptyState
                            title="No drill collars in this project yet."
                            detail="The map renders collar locations, uncertainty rings, and coverage density once collars are ingested. Upload drill logs or connect a data source to populate it."
                            action={
                                <Link
                                    href={`/projects/${project.slug}/sources`}
                                    className="inline-block text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                    style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                                >
                                    Open Data →
                                </Link>
                            }
                        />
                    </section>
                )}
            </div>
        </AppLayout>
    );
}
