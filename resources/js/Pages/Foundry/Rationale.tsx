import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, EmptyState } from '@/Components/Foundry/primitives';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';
import type { RationaleProps } from '@/Types/Foundry';

/**
 * Foundry Rationale — "Why this target?" narrative surface.
 * Reads from targeting.target_score_factors via FoundryRationaleController.
 */
export default function FoundryRationale({
    target_id,
    project,
    rank,
    coord,
    confidence,
    summary,
    positives,
    negatives,
    analogues,
    deposit_model_slug,
    empty,
}: RationaleProps) {
    // Phase 6 — score_targets / train_target_model re-runs rebuild the
    // factor list this page renders. Filter on 'targets' (the Phase 1
    // affected_type emitted by score_targets) — same as Foundry/Targets.
    useWorkspaceDataUpdated(project.project_id, (event) => {
        if (event.affected_types.includes('targets')) {
            router.reload({
                only: ['rank', 'coord', 'confidence', 'summary', 'positives', 'negatives', 'analogues', 'deposit_model_slug', 'empty'],
            });
        }
    });

    return (
        <AppLayout>
            <Head title={`Rationale · ${target_id}`} />

            <div className="flex-1 overflow-y-auto" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · RATIONALE`}
                    title={`Why this target?`}
                    sub={
                        <span className="font-mono">
                            {target_id}
                            {rank !== null && <span> · rank #{rank}</span>}
                            {confidence !== null && <span> · confidence {confidence.toFixed(2)}</span>}
                            {coord && <span> · {coord}</span>}
                            {deposit_model_slug && <span> · {deposit_model_slug}</span>}
                        </span>
                    }
                    actions={
                        <Link
                            href={`/projects/${project.slug}/targets`}
                            className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                            style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                        >
                            ← Targets
                        </Link>
                    }
                />

                {empty ? (
                    <div className="px-8 py-12">
                        <EmptyState
                            title="No rationale recorded for this target yet."
                            detail="Run the targeting pipeline to generate evidence-stack + analog comparison + confidence trajectory. Once stored in targeting.target_score_factors, this surface populates automatically."
                            action={
                                <Link
                                    href={`/projects/${project.slug}/targets`}
                                    className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                    style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                                >
                                    Back to targets
                                </Link>
                            }
                        />
                    </div>
                ) : (
                    <section className="px-8 py-6 grid grid-cols-1 lg:grid-cols-2 gap-4">
                        {summary && (
                            <Card eyebrow="SUMMARY" className="lg:col-span-2">
                                <p className="text-sm leading-relaxed" style={{ color: 'var(--fg-1)' }}>{summary}</p>
                            </Card>
                        )}

                        <Card eyebrow={`POSITIVE INDICATORS · ${positives.length}`}>
                            {positives.length === 0 ? (
                                <div className="text-xs" style={{ color: 'var(--fg-3)' }}>No positive factors recorded.</div>
                            ) : (
                                <ul className="space-y-3">
                                    {positives.map((p, i) => (
                                        <li key={i} className="text-xs">
                                            <div className="flex items-start gap-2 mb-1">
                                                <Pill tone="accent">+{p.weight.toFixed(2)}</Pill>
                                                <span className="font-medium" style={{ color: 'var(--fg-0)' }}>{p.factor}</span>
                                            </div>
                                            <div style={{ color: 'var(--fg-2)', paddingLeft: 60 }}>{p.detail}</div>
                                        </li>
                                    ))}
                                </ul>
                            )}
                        </Card>

                        <Card eyebrow={`NEGATIVE INDICATORS · ${negatives.length}`}>
                            {negatives.length === 0 ? (
                                <div className="text-xs" style={{ color: 'var(--fg-3)' }}>No negative factors recorded.</div>
                            ) : (
                                <ul className="space-y-3">
                                    {negatives.map((p, i) => (
                                        <li key={i} className="text-xs">
                                            <div className="flex items-start gap-2 mb-1">
                                                <Pill tone="warn">{p.weight.toFixed(2)}</Pill>
                                                <span className="font-medium" style={{ color: 'var(--fg-0)' }}>{p.factor}</span>
                                            </div>
                                            <div style={{ color: 'var(--fg-2)', paddingLeft: 60 }}>{p.detail}</div>
                                        </li>
                                    ))}
                                </ul>
                            )}
                        </Card>

                        {analogues.length > 0 && (
                            <Card eyebrow="ANALOGUES" className="lg:col-span-2">
                                <table className="w-full text-xs">
                                    <thead>
                                        <tr style={{ color: 'var(--fg-3)' }}>
                                            <th className="text-left font-mono uppercase tracking-wider py-1">Deposit</th>
                                            <th className="text-left font-mono uppercase tracking-wider py-1">Similarity</th>
                                            <th className="text-left font-mono uppercase tracking-wider py-1">Geometry</th>
                                            <th className="text-left font-mono uppercase tracking-wider py-1">Grade</th>
                                            <th className="text-left font-mono uppercase tracking-wider py-1">Source</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {analogues.map((a, i) => (
                                            <tr key={i} className="border-t" style={{ borderColor: 'var(--line-1)' }}>
                                                <td className="py-1.5" style={{ color: 'var(--fg-0)' }}>{a.name}</td>
                                                <td className="py-1.5 font-mono" style={{ color: 'var(--accent)' }}>{(a.similarity * 100).toFixed(0)}%</td>
                                                <td className="py-1.5" style={{ color: 'var(--fg-1)' }}>{a.geometry}</td>
                                                <td className="py-1.5 font-mono" style={{ color: 'var(--fg-1)' }}>{a.grade ?? '—'}</td>
                                                <td className="py-1.5" style={{ color: 'var(--fg-3)' }}>{a.source}</td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </Card>
                        )}
                    </section>
                )}
            </div>
        </AppLayout>
    );
}
