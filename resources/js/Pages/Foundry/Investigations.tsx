import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, EmptyState } from '@/Components/Foundry/primitives';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';

interface Investigation {
    id: string;
    title: string;
    updated: string;
    pinned: boolean;
}

interface InvestigationsProps {
    project: { project_id: string; project_name: string; slug: string };
    investigations: Investigation[];
    empty: boolean;
}

export default function FoundryInvestigations({ project, investigations, empty }: InvestigationsProps) {
    // Phase 3 real-time push — ChatConversationController::upsert dispatches
    // WorkspaceDataUpdated affected_types=['investigations'] whenever a new
    // chat conversation is created or an existing one's project_id changes.
    useWorkspaceDataUpdated(project.project_id, (event) => {
        if (event.affected_types.includes('investigations')) {
            router.reload({ only: ['investigations', 'empty'] });
        }
    });

    return (
        <AppLayout>
            <Head title={`Investigations · ${project.project_name}`} />

            <div className="flex-1 overflow-y-auto" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · INVESTIGATIONS`}
                    title="Saved hypothesis threads"
                    sub={`${investigations.length} thread${investigations.length === 1 ? '' : 's'} pinned for this project`}
                    actions={
                        <Link href={`/threads?project=${project.project_id}`} className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border" style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}>
                            + New investigation
                        </Link>
                    }
                />

                {empty ? (
                    <div className="px-8 py-12">
                        <EmptyState title="No investigations saved yet." detail="Pin a chat thread as an investigation to keep its full retrieval trail, citations, and any forks." />
                    </div>
                ) : (
                    <section className="px-8 py-6 space-y-2 max-w-3xl">
                        {investigations.map((i) => (
                            <Link
                                key={i.id}
                                href={`/threads?thread=${i.id}`}
                                className="block p-3 rounded-md border transition-colors hover:bg-[var(--bg-hover)]"
                                style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)' }}
                            >
                                <div className="flex items-center gap-2">
                                    {i.pinned && <Pill tone="accent">pinned</Pill>}
                                    <span className="text-sm font-medium" style={{ color: 'var(--fg-0)' }}>{i.title}</span>
                                    <span className="ml-auto text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>{i.updated.slice(0, 16)}</span>
                                </div>
                            </Link>
                        ))}
                    </section>
                )}
            </div>
        </AppLayout>
    );
}
