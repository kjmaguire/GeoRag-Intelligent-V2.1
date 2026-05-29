import { useState } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, EmptyState } from '@/Components/Foundry/primitives';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';
import type { TargetsProps } from '@/Types/Foundry';

/**
 * Foundry Targets — drill-target recommendation surface (§8).
 *
 * Reads targeting.target_models + target_recommendations from the
 * `roll_front_uranium` active deposit model (Wyoming Cameco Shirley Basin).
 */
export default function FoundryTargets({ project, deposit_models, active_model_slug, recommendations, empty }: TargetsProps) {
    const [selectedId, setSelectedId] = useState<string | null>(recommendations[0]?.target_id ?? null);
    const selected = recommendations.find((r) => r.target_id === selectedId) ?? null;

    // Reliability spec Phase 2b — score_targets workflow POSTs to
    // /api/internal/v1/workspace-data-updated with affected_types=['targets']
    // on success. The WorkspaceDataUpdated event lands on
    // project.{projectId}.ingestion and this hook fires a partial reload
    // so a finished scoring run surfaces without a manual refresh.
    useWorkspaceDataUpdated(project.project_id, (event) => {
        if (event.affected_types.includes('targets')) {
            router.reload({
                only: ['deposit_models', 'active_model_slug', 'recommendations', 'empty'],
            });
        }
    });

    return (
        <AppLayout>
            <Head title={`Targets · ${project.project_name}`} />

            <div className="flex-1 overflow-hidden" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()}`}
                    title="Drill targets"
                    sub={
                        active_model_slug
                            ? `Active deposit model: ${active_model_slug}`
                            : 'No deposit model active for this project.'
                    }
                />

                <div className="grid grid-cols-1 lg:grid-cols-[260px_1fr_360px] h-[calc(100%-90px)]">
                    {/* Deposit model rail */}
                    <aside className="border-r overflow-y-auto" style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}>
                        <div className="px-4 pt-4 pb-2 text-[10px] font-mono uppercase tracking-[0.12em]" style={{ color: 'var(--fg-3)' }}>
                            Deposit models
                        </div>
                        {deposit_models.map((m) => (
                            <div
                                key={m.slug}
                                className="px-4 py-2.5 border-b text-xs"
                                style={{
                                    borderColor: 'var(--line-1)',
                                    background: m.is_active ? 'var(--accent-bg)' : 'transparent',
                                    color: m.is_active ? 'var(--fg-0)' : 'var(--fg-2)',
                                    opacity: m.is_active ? 1 : 0.5,
                                }}
                            >
                                <div className="flex items-center gap-2 mb-1">
                                    <span className="font-medium">{m.display_name}</span>
                                    {m.is_active && <Pill tone="accent" dot>active</Pill>}
                                </div>
                                <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                                    {m.commodity_primary} · {m.slug}
                                </div>
                            </div>
                        ))}
                    </aside>

                    {/* Ranked list */}
                    <section className="overflow-y-auto px-6 py-5">
                        <div className="text-[10px] font-mono uppercase tracking-[0.12em] mb-3" style={{ color: 'var(--fg-3)' }}>
                            Ranked recommendations
                        </div>
                        {empty ? (
                            <EmptyState
                                title="No target recommendations yet."
                                detail="The §8 LangGraph (deposit_model → matcher → scorer → constraint check) runs nightly. Trigger a run from the Admin → Target Recommendation cockpit, or wait for the next scheduled run."
                            />
                        ) : (
                            <div className="flex flex-col gap-2">
                                {recommendations.map((r) => (
                                    <button
                                        key={r.target_id}
                                        type="button"
                                        onClick={() => setSelectedId(r.target_id)}
                                        className="w-full text-left p-3 rounded border transition-colors hover:bg-[var(--bg-hover)]"
                                        style={{
                                            background: selectedId === r.target_id ? 'var(--bg-2)' : 'var(--bg-1)',
                                            borderColor: selectedId === r.target_id ? 'var(--accent-dim)' : 'var(--line-1)',
                                        }}
                                    >
                                        <div className="flex items-baseline gap-3">
                                            <span
                                                className="w-6 h-6 rounded text-[11px] font-mono font-semibold flex items-center justify-center"
                                                style={{ background: 'var(--accent-bg)', color: 'var(--accent)' }}
                                            >
                                                {r.rank}
                                            </span>
                                            <span className="text-sm font-medium font-mono" style={{ color: 'var(--fg-0)' }}>
                                                {r.target_id.slice(0, 8)}
                                            </span>
                                            <Pill tone="accent" dot>
                                                score {r.score.toFixed(2)}
                                            </Pill>
                                            <Pill tone="info">
                                                conf {r.confidence.toFixed(2)}
                                            </Pill>
                                        </div>
                                        <div className="text-xs mt-2" style={{ color: 'var(--fg-2)' }}>
                                            {r.summary || <em style={{ color: 'var(--fg-3)' }}>(no summary)</em>}
                                        </div>
                                    </button>
                                ))}
                            </div>
                        )}
                    </section>

                    {/* Detail rail */}
                    <aside className="border-l overflow-y-auto p-5" style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}>
                        {selected ? (
                            <div className="flex flex-col gap-4">
                                <div>
                                    <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                                        Target
                                    </div>
                                    <div className="text-base font-medium font-mono mt-1" style={{ color: 'var(--fg-0)' }}>
                                        {selected.target_id}
                                    </div>
                                </div>
                                <Card eyebrow="Positives" title={`${selected.positives.length} factors`}>
                                    {selected.positives.length === 0 ? (
                                        <div className="text-xs" style={{ color: 'var(--fg-3)' }}>None recorded.</div>
                                    ) : (
                                        <ul className="text-xs space-y-2">
                                            {selected.positives.map((p, i) => (
                                                <li key={i} className="flex items-start gap-2">
                                                    <span style={{ color: 'var(--accent)' }}>+{p.weight.toFixed(2)}</span>
                                                    <span style={{ color: 'var(--fg-1)' }}>{p.detail}</span>
                                                </li>
                                            ))}
                                        </ul>
                                    )}
                                </Card>
                                <div className="flex gap-2">
                                    <Link
                                        href={`/projects/${project.slug}/targets/${selected.target_id}/rationale`}
                                        className="flex-1 text-center text-xs font-mono uppercase tracking-wider px-3 py-2 rounded border"
                                        style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                                    >
                                        Why this target?
                                    </Link>
                                </div>
                            </div>
                        ) : (
                            <div className="text-xs text-center pt-12" style={{ color: 'var(--fg-3)' }}>
                                Select a target to view its rationale.
                            </div>
                        )}
                    </aside>
                </div>
            </div>
        </AppLayout>
    );
}
