import { useState } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, Stat, EmptyState, Segmented } from '@/Components/Foundry/primitives';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';
import type { SupportCockpitProps } from '@/Types/Foundry';

type Section = 'traces' | 'eval' | 'thresholds';

export default function FoundrySupportCockpit({ workspaces, traces, thresholds, can_admin, empty }: SupportCockpitProps) {
    const [section, setSection] = useState<Section>('traces');
    const [wsId, setWsId] = useState<string | null>(workspaces[0]?.id ?? null);

    // Phase 3 real-time push — the support_replay workflow broadcasts to
    // admin.support-cockpit on completion. Reuses the Phase 2 admin
    // broadcast infrastructure (this page is admin-gated, same as the
    // Admin/* surfaces). Reload only the traces list; workspaces +
    // thresholds change on a different cadence.
    useAdminSurfaceUpdated('support-cockpit', null, () => {
        router.reload({ only: ['traces'] });
    });

    const filteredTraces = traces.filter((t) => !wsId || t.workspace_id === wsId);

    if (!can_admin) {
        return (
            <AppLayout>
                <Head title="Support Cockpit" />
                <div className="flex-1 flex items-center justify-center" style={{ background: 'var(--bg-0)' }}>
                    <EmptyState
                        title="Admin only"
                        detail="The Support Cockpit is restricted to workspace administrators."
                    />
                </div>
            </AppLayout>
        );
    }

    return (
        <AppLayout>
            <Head title="Support Cockpit" />

            <div className="flex-1 overflow-hidden" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow="§10 SUPPORT COCKPIT"
                    title="Operator replay + threshold tuning"
                    sub={`${workspaces.length} workspaces · ${traces.length} recent traces`}
                />

                {empty ? (
                    <div className="px-8 py-12">
                        <EmptyState
                            title="No workspaces or traces visible."
                            detail="Once a workspace is provisioned and queries flow, traces appear here for replay and audit."
                        />
                    </div>
                ) : (
                    <div className="grid grid-cols-[280px_1fr] h-[calc(100%-90px)]">
                        <aside className="border-r overflow-y-auto" style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}>
                            <div className="px-4 pt-4 pb-2 text-[10px] font-mono uppercase tracking-[0.12em]" style={{ color: 'var(--fg-3)' }}>
                                Workspaces
                            </div>
                            {workspaces.map((w) => (
                                <button
                                    key={w.id}
                                    type="button"
                                    onClick={() => setWsId(w.id)}
                                    className="w-full text-left px-4 py-2.5 border-b text-xs transition-colors"
                                    style={{
                                        borderColor: 'var(--line-1)',
                                        background: wsId === w.id ? 'var(--accent-bg)' : 'transparent',
                                        color: wsId === w.id ? 'var(--fg-0)' : 'var(--fg-2)',
                                    }}
                                >
                                    <div className="font-medium">{w.name}</div>
                                    <div className="text-[10px] font-mono mt-0.5" style={{ color: 'var(--fg-3)' }}>
                                        {w.region} · {w.plan}
                                    </div>
                                </button>
                            ))}
                        </aside>

                        <section className="overflow-y-auto">
                            <div className="px-6 py-3 border-b flex items-center gap-3" style={{ borderColor: 'var(--line-1)' }}>
                                <Segmented<Section>
                                    value={section}
                                    onChange={setSection}
                                    options={[
                                        { value: 'traces', label: 'Traces' },
                                        { value: 'eval', label: 'Eval' },
                                        { value: 'thresholds', label: 'Thresholds' },
                                    ]}
                                />
                            </div>

                            {section === 'traces' && (
                                <div className="px-6 py-5">
                                    <Card eyebrow={`TRACES · ${filteredTraces.length}`} padded={false}>
                                        {filteredTraces.length === 0 ? (
                                            <div className="px-4 py-6 text-xs" style={{ color: 'var(--fg-3)' }}>No traces for this workspace.</div>
                                        ) : (
                                            filteredTraces.map((t) => (
                                                <div key={t.run_id} className="grid grid-cols-[80px_1fr_70px_70px_60px] gap-3 items-center px-4 py-2 border-b" style={{ borderColor: 'var(--line-1)' }}>
                                                    <Pill tone={t.status === 'ok' ? 'accent' : t.status === 'refused' ? 'warn' : 'danger'} dot>{t.status}</Pill>
                                                    <Link href={`/retrieval/${t.run_id}`} className="text-xs truncate hover:underline" style={{ color: 'var(--fg-0)' }}>
                                                        {t.question || <em style={{ color: 'var(--fg-3)' }}>(empty)</em>}
                                                    </Link>
                                                    <span className="text-[10px] font-mono" style={{ color: 'var(--fg-2)' }}>{t.when}</span>
                                                    <span className="text-[10px] font-mono text-right" style={{ color: 'var(--fg-2)' }}>{t.latency_ms}ms</span>
                                                    <span className="text-[10px] font-mono text-right" style={{ color: 'var(--accent)' }}>{t.confidence.toFixed(2)}</span>
                                                </div>
                                            ))
                                        )}
                                    </Card>
                                </div>
                            )}

                            {section === 'eval' && (
                                <div className="px-6 py-5">
                                    <Card eyebrow="EVAL" title="Workspace eval scores">
                                        <div className="text-xs" style={{ color: 'var(--fg-2)' }}>
                                            Eval scores come from eval.golden_questions runs (§10.4 evaluate_workspace).
                                            Admin → Eval Dashboard is the operator surface for golden-question editing.
                                        </div>
                                        <Link
                                            href="/admin/eval-dashboard"
                                            className="inline-block mt-3 text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                            style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                                        >
                                            Open Eval Dashboard →
                                        </Link>
                                    </Card>
                                </div>
                            )}

                            {section === 'thresholds' && (
                                <div className="px-6 py-5">
                                    <Card eyebrow="THRESHOLDS" title="Workspace gate values" padded={false}>
                                        {thresholds.map((t) => (
                                            <div key={t.id} className="px-4 py-3 border-b grid grid-cols-[1fr_120px] gap-3 items-center" style={{ borderColor: 'var(--line-1)' }}>
                                                <div>
                                                    <div className="text-xs font-medium" style={{ color: 'var(--fg-0)' }}>{t.label}</div>
                                                    <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>{t.id}</div>
                                                </div>
                                                <div className="text-right">
                                                    <span className="text-sm font-mono" style={{ color: 'var(--accent)' }}>{t.value}{t.unit}</span>
                                                    <div className="text-[10px] font-mono" style={{ color: 'var(--fg-3)' }}>{t.min_value}–{t.max_value}</div>
                                                </div>
                                            </div>
                                        ))}
                                    </Card>
                                </div>
                            )}
                        </section>
                    </div>
                )}
            </div>
        </AppLayout>
    );
}
