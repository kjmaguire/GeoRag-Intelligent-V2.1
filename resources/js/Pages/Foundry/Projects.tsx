import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Pill, StatusDot, EmptyState } from '@/Components/Foundry/primitives';
import { useWorkspaceActivity } from '@/Hooks/useWorkspaceActivity';
import type { ProjectsIndexProps, ProjectStatus } from '@/Types/Foundry';

/**
 * Foundry Projects — list/picker view of all projects in the workspace.
 * Separate from Portfolio (which is the exec rollup dashboard).
 */
export default function FoundryProjects({ workspace_id, projects, empty }: ProjectsIndexProps) {
    // Phase 3 real-time push — ProjectController.{store, update, destroy}
    // dispatches WorkspaceActivityBroadcast with affected_types=['projects','kpis'].
    // Filter on 'projects' so we don't reload every time KPIs drift.
    useWorkspaceActivity(workspace_id, (event) => {
        if (event.affected_types.includes('projects')) {
            router.reload({ only: ['projects', 'empty'] });
        }
    });

    return (
        <AppLayout>
            <Head title="Projects — GeoRAG" />

            <div className="flex-1 overflow-y-auto" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow="ORG · PROJECTS"
                    title="All projects"
                    sub={`${projects.length} ${projects.length === 1 ? 'project' : 'projects'}`}
                    actions={
                        <Link
                            href="/foundry/projects/new"
                            className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                            style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                        >
                            + New project
                        </Link>
                    }
                />

                {empty ? (
                    <div className="px-8 py-12">
                        <EmptyState
                            title="No projects yet."
                            detail="Create your first project to start ingesting drill logs, geophysics, and reports."
                            action={
                                <Link
                                    href="/foundry/projects/new"
                                    className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                    style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                                >
                                    + Create first project
                                </Link>
                            }
                        />
                    </div>
                ) : (
                    <section className="px-8 py-6 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                        {projects.map((p) => (
                            <Link
                                key={p.project_id}
                                href={`/projects/${p.slug}`}
                                className="block p-4 rounded-md border transition-colors hover:bg-[var(--bg-hover)]"
                                style={{ background: 'var(--bg-1)', borderColor: 'var(--line-2)' }}
                            >
                                <div className="flex items-center gap-2 mb-2">
                                    <StatusDot status={p.status} />
                                    <span className="text-[9px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                                        {p.commodity ?? '—'}
                                    </span>
                                    {p.status !== 'active' && <Pill tone={statusTone(p.status)}>{p.status}</Pill>}
                                </div>
                                <h3 className="text-base font-semibold leading-tight mb-1" style={{ color: 'var(--fg-0)' }}>
                                    {p.project_name}
                                </h3>
                                <div className="text-[11px]" style={{ color: 'var(--fg-3)' }}>{p.region ?? '—'}</div>
                                <div className="mt-3 flex items-center gap-2 text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                                    <span>EPSG:{p.crs_epsg ?? '—'}</span>
                                    <span>·</span>
                                    <span>v{p.data_version}</span>
                                </div>
                            </Link>
                        ))}
                    </section>
                )}
            </div>
        </AppLayout>
    );
}

function statusTone(s: ProjectStatus): 'accent' | 'warn' | 'neutral' | 'info' | 'danger' {
    if (s === 'active') return 'accent';
    if (s === 'indexing') return 'info';
    if (s === 'degraded') return 'warn';
    if (s === 'archived') return 'neutral';
    return 'neutral';
}
