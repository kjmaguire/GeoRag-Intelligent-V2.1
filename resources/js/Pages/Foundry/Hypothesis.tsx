import { Head, Link } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, EmptyState } from '@/Components/Foundry/primitives';

// Reuses the same controller as Reasoning but renders the original 3-pane
// HypothesisPage layout for users who prefer the dense view.

interface Hypothesis { id: string; title: string; status: string; support_count: number; confidence: number | null; updated: string }
interface Evidence { id: string; title: string; src: string; score: number | null; pinned: boolean }

export default function FoundryHypothesis({ project, hypotheses, evidence, empty }: {
    project: { project_id: string; project_name: string; slug: string };
    hypotheses: Hypothesis[];
    evidence: Evidence[];
    empty: boolean;
}) {
    return (
        <AppLayout>
            <Head title={`Hypothesis · ${project.project_name}`} />

            <div className="flex-1 flex flex-col overflow-hidden" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · HYPOTHESIS · 3-PANE`}
                    title="Data explorer × reasoning chain × synthesis"
                    sub="The dense 3-column view (Reasoning workbench is the cleaner 4-stage alternative)"
                    actions={
                        <Link href={`/projects/${project.slug}/reasoning`} className="text-[10px] font-mono uppercase tracking-wider px-3 py-1.5 rounded border" style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}>
                            → Open Reasoning (4-stage)
                        </Link>
                    }
                />

                {empty ? (
                    <div className="px-8 py-12"><EmptyState title="No hypotheses or evidence yet." /></div>
                ) : (
                    <div className="flex-1 grid grid-cols-3 gap-px overflow-hidden" style={{ background: 'var(--line-1)' }}>
                        {/* Corpus picker */}
                        <section className="overflow-y-auto" style={{ background: 'var(--bg-1)' }}>
                            <div className="px-4 py-3 border-b text-[10px] font-mono uppercase tracking-[0.12em] sticky top-0" style={{ borderColor: 'var(--line-1)', color: 'var(--fg-3)', background: 'var(--bg-1)' }}>
                                Corpus picker · {evidence.length}
                            </div>
                            {evidence.length === 0 ? (
                                <div className="px-4 py-6 text-xs" style={{ color: 'var(--fg-3)' }}>No evidence indexed.</div>
                            ) : evidence.map((e) => (
                                <div key={e.id} className="px-4 py-2 border-b text-xs" style={{ borderColor: 'var(--line-1)' }}>
                                    <div className="flex items-center gap-2 mb-0.5">
                                        <input type="checkbox" defaultChecked={e.pinned} />
                                        <span className="font-mono text-[10px] uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>{e.src}</span>
                                        <span className="ml-auto font-mono text-[10px]" style={{ color: e.score !== null && e.score >= 0.7 ? 'var(--accent)' : 'var(--fg-3)' }}>{e.score?.toFixed(2) ?? '—'}</span>
                                    </div>
                                    <div style={{ color: 'var(--fg-1)' }}>{e.title}</div>
                                </div>
                            ))}
                        </section>

                        {/* Reasoning chain */}
                        <section className="overflow-y-auto" style={{ background: 'var(--bg-0)' }}>
                            <div className="px-4 py-3 border-b text-[10px] font-mono uppercase tracking-[0.12em] sticky top-0" style={{ borderColor: 'var(--line-1)', color: 'var(--fg-3)', background: 'var(--bg-0)' }}>
                                Reasoning chain · {hypotheses.length} steps
                            </div>
                            <ol className="px-4 py-3 space-y-3 text-xs">
                                {hypotheses.map((h, i) => (
                                    <li key={h.id} className="grid grid-cols-[24px_1fr] gap-3">
                                        <span className="w-6 h-6 rounded-full text-[10px] font-mono flex items-center justify-center" style={{ background: 'var(--accent-bg)', color: 'var(--accent)' }}>{i + 1}</span>
                                        <div>
                                            <div style={{ color: 'var(--fg-0)' }}>{h.title}</div>
                                            <div className="flex items-center gap-2 mt-1">
                                                <Pill tone={h.status === 'accepted' ? 'accent' : 'neutral'} dot>{h.status}</Pill>
                                                <span className="text-[10px] font-mono" style={{ color: 'var(--fg-3)' }}>{h.support_count} support</span>
                                            </div>
                                        </div>
                                    </li>
                                ))}
                            </ol>
                        </section>

                        {/* Synthesis panel */}
                        <section className="overflow-y-auto" style={{ background: 'var(--bg-1)' }}>
                            <div className="px-4 py-3 border-b text-[10px] font-mono uppercase tracking-[0.12em] sticky top-0" style={{ borderColor: 'var(--line-1)', color: 'var(--fg-3)', background: 'var(--bg-1)' }}>
                                Synthesis · candidates
                            </div>
                            {hypotheses.slice(0, 5).map((h) => (
                                <Card key={h.id} eyebrow={
                                    <span className="flex items-center gap-2">
                                        <Pill tone="info">{h.confidence !== null ? h.confidence.toFixed(2) : '—'}</Pill>
                                        <span>{h.status}</span>
                                    </span>
                                } title={h.title} className="m-3">
                                    <div className="text-[11px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                                        {h.support_count} support · {h.updated.slice(0, 10)}
                                    </div>
                                </Card>
                            ))}
                        </section>
                    </div>
                )}
            </div>
        </AppLayout>
    );
}
