import type { JSX } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';
import { StatCard, SectionCard, DataTable, EmptyState } from './_shared';

interface PageProps {
    recent_recommendations: {
        recommendation_id: string; project_id: string; run_id: string;
        rank: number; created_at: string; explanation_preview: string;
    }[];
    by_project: {
        project_name: string; rec_count: number; last_run: string | null;
    }[];
}

function fmtDate(iso: string | null): string {
    if (!iso) return '—';
    return new Date(iso).toISOString().slice(0, 10);
}

export default function TargetRecommendation({ recent_recommendations, by_project }: PageProps): JSX.Element {
    // Phase 6 — reuses the Phase 2 admin.target-recommendation channel.
    // score_targets broadcasts on every successful TRG run; same dispatch
    // already fires for the Admin/TargetRecommendationRuns sibling page.
    useAdminSurfaceUpdated('target-recommendation', null, () => {
        router.reload({ only: ['recent_recommendations', 'by_project'] });
    });

    const totalRecs = recent_recommendations.length;
    const totalProjects = by_project.length;

    return (
        <AppLayout>
            <Head title="Target Recommendation" />
            <div className="mx-auto max-w-7xl space-y-6 px-4 py-6 sm:px-6 lg:px-8">
                <div className="flex items-center justify-between">
                    <div>
                        <h1 className="text-2xl font-semibold text-zinc-900">Target Recommendation</h1>
                        <p className="mt-1 text-sm text-zinc-500">§16.1 · TRG run history per project</p>
                    </div>
                    <Link
                        href="/admin/target-recommendation-cockpit"
                        className="rounded bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500"
                    >
                        Open TRG cockpit →
                    </Link>
                </div>

                <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                    <StatCard label="Recent recommendations" value={totalRecs} sub="across all projects" />
                    <StatCard label="Projects with TRG runs" value={totalProjects} />
                    <StatCard label="Top rank" value={recent_recommendations[0]?.rank ?? '—'} />
                </div>

                <SectionCard title="Per-project rollup">
                    {by_project.length === 0 ? (
                        <EmptyState message="No TRG runs yet — kick one off from the cockpit." />
                    ) : (
                        <DataTable
                            rows={by_project}
                            columns={[
                                { key: 'project_name', label: 'Project' },
                                { key: 'rec_count', label: 'Recommendations' },
                                { key: 'last_run', label: 'Last run', render: (v) => fmtDate(v as string | null) },
                            ]}
                        />
                    )}
                </SectionCard>

                <SectionCard title="Recent recommendations" sub="latest 25, all projects">
                    <DataTable
                        rows={recent_recommendations}
                        columns={[
                            { key: 'rank', label: 'Rank' },
                            { key: 'created_at', label: 'When', render: (v) => fmtDate(v as string) },
                            { key: 'explanation_preview', label: 'Explanation' },
                        ]}
                    />
                </SectionCard>
            </div>
        </AppLayout>
    );
}
