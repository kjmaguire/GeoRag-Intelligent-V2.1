import { Head } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, EmptyState } from '@/Components/Foundry/primitives';
import type { SavedMapViewsProps } from '@/Types/Foundry';

const SCOPE_TONE: Record<string, 'accent' | 'info' | 'neutral'> = {
    user: 'neutral',
    project: 'info',
    workspace: 'accent',
};

export default function FoundrySavedMapViews({ project_id, views, empty }: SavedMapViewsProps) {
    return (
        <AppLayout>
            <Head title="Saved map views" />

            <div className="flex-1 overflow-y-auto" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow="§6.5 SAVED MAP VIEWS"
                    title="Bookmarked map states"
                    sub={`${views.length} view${views.length === 1 ? '' : 's'} across user / project / workspace scope`}
                />

                {empty ? (
                    <div className="px-8 py-12">
                        <EmptyState
                            title="No saved map views yet."
                            detail="Save the current Workspace MAP-mode view to bookmark it. Choose scope: Just me, Project (visible to project members), or Workspace (visible to all workspace members)."
                        />
                    </div>
                ) : (
                    <section className="px-8 py-6 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
                        {views.map((v) => (
                            <Card key={v.id} eyebrow={
                                <span className="flex items-center gap-2">
                                    <Pill tone={SCOPE_TONE[v.scope] ?? 'neutral'}>{v.scope}</Pill>
                                    <span>{v.owner}</span>
                                </span>
                            } title={v.name}>
                                <div className="grid grid-cols-2 gap-2 text-[11px] font-mono" style={{ color: 'var(--fg-2)' }}>
                                    <div>BASEMAP <span style={{ color: 'var(--fg-0)' }}>{v.basemap}</span></div>
                                    <div>LAYERS <span style={{ color: 'var(--fg-0)' }}>{v.layers_count}</span></div>
                                    <div className="col-span-2">VIEWPORT <span style={{ color: 'var(--fg-0)' }}>{v.viewport}</span></div>
                                    <div className="col-span-2">UPDATED <span style={{ color: 'var(--fg-0)' }}>{v.updated}</span></div>
                                </div>
                            </Card>
                        ))}
                    </section>
                )}
            </div>
        </AppLayout>
    );
}
