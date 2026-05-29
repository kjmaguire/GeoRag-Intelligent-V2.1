import { useState } from 'react';
import { Head, useForm } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, EmptyState } from '@/Components/Foundry/primitives';

interface DecisionsProps {
    project: { project_id: string; project_name: string; slug: string };
    decisions: Array<{
        decision_id: string;
        title: string;
        kind: string;
        subject: string;
        rationale: string;
        outcome: string;
        created_by: string;
        created_at: string;
        audit_anchor: string;
    }>;
    empty: boolean;
}

export default function FoundryDecisions({ project, decisions, empty }: DecisionsProps) {
    const [composing, setComposing] = useState(false);
    const { data, setData, post, processing, reset } = useForm({
        title: '',
        kind: 'manual',
        subject: '',
        rationale: '',
        outcome: 'accepted',
    });

    function submit(e: React.FormEvent) {
        e.preventDefault();
        post(`/projects/${project.slug}/decisions`, {
            preserveScroll: true,
            onSuccess: () => {
                reset();
                setComposing(false);
            },
        });
    }

    return (
        <AppLayout>
            <Head title={`Decisions · ${project.project_name}`} />

            <div className="flex-1 overflow-y-auto" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · §9.9 DECISIONS`}
                    title="Decision intelligence"
                    sub={`${decisions.length} recorded decision${decisions.length === 1 ? '' : 's'}`}
                    actions={
                        <button
                            type="button"
                            onClick={() => setComposing((v) => !v)}
                            className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                            style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                        >
                            {composing ? 'Cancel' : '+ Record decision'}
                        </button>
                    }
                />

                {composing && (
                    <section className="px-8 py-5">
                        <Card eyebrow="NEW DECISION" title="Capture rationale + outcome">
                            <form onSubmit={submit} className="space-y-3">
                                <FormRow label="Title">
                                    <input
                                        type="text"
                                        value={data.title}
                                        onChange={(e) => setData('title', e.target.value)}
                                        className="w-full text-xs px-2 py-1.5 rounded border bg-transparent"
                                        style={{ background: 'var(--bg-2)', color: 'var(--fg-0)', borderColor: 'var(--line-2)' }}
                                        required
                                    />
                                </FormRow>
                                <FormRow label="Kind">
                                    <select
                                        value={data.kind}
                                        onChange={(e) => setData('kind', e.target.value)}
                                        className="text-xs px-2 py-1.5 rounded border"
                                        style={{ background: 'var(--bg-2)', color: 'var(--fg-0)', borderColor: 'var(--line-2)' }}
                                    >
                                        <option value="drill_target">Accept drill target</option>
                                        <option value="report_approved">Approve report</option>
                                        <option value="threshold_change">Change workspace threshold</option>
                                        <option value="source_promoted">Promote source to silver</option>
                                        <option value="hypothesis_accept">Accept hypothesis</option>
                                        <option value="query_pin">Pin retrieval as canonical</option>
                                        <option value="manual">Manual</option>
                                    </select>
                                </FormRow>
                                <FormRow label="Subject">
                                    <input
                                        type="text"
                                        value={data.subject}
                                        onChange={(e) => setData('subject', e.target.value)}
                                        className="w-full text-xs px-2 py-1.5 rounded border"
                                        style={{ background: 'var(--bg-2)', color: 'var(--fg-0)', borderColor: 'var(--line-2)' }}
                                    />
                                </FormRow>
                                <FormRow label="Rationale">
                                    <textarea
                                        value={data.rationale}
                                        onChange={(e) => setData('rationale', e.target.value)}
                                        rows={4}
                                        className="w-full text-xs px-2 py-1.5 rounded border resize-none"
                                        style={{ background: 'var(--bg-2)', color: 'var(--fg-0)', borderColor: 'var(--line-2)' }}
                                    />
                                </FormRow>
                                <FormRow label="Outcome">
                                    <select
                                        value={data.outcome}
                                        onChange={(e) => setData('outcome', e.target.value)}
                                        className="text-xs px-2 py-1.5 rounded border"
                                        style={{ background: 'var(--bg-2)', color: 'var(--fg-0)', borderColor: 'var(--line-2)' }}
                                    >
                                        <option value="accepted">Accepted</option>
                                        <option value="rejected">Rejected</option>
                                        <option value="deferred">Deferred</option>
                                    </select>
                                </FormRow>
                                <button
                                    type="submit"
                                    disabled={processing || !data.title}
                                    className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border disabled:opacity-40"
                                    style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                                >
                                    {processing ? 'Saving…' : 'Commit decision'}
                                </button>
                            </form>
                        </Card>
                    </section>
                )}

                {empty && !composing ? (
                    <div className="px-8 py-12">
                        <EmptyState
                            title="No decisions recorded yet."
                            detail="Capture decisions as they're made — accepting a drill target, approving a report, promoting a source, etc. Each entry is anchored to the audit ledger."
                            action={
                                <button
                                    type="button"
                                    onClick={() => setComposing(true)}
                                    className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                    style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                                >
                                    + Record first decision
                                </button>
                            }
                        />
                    </div>
                ) : (
                    <section className="px-8 py-6 space-y-3 max-w-4xl">
                        {decisions.map((d) => (
                            <Card key={d.decision_id} eyebrow={
                                <span className="flex items-center gap-2">
                                    <Pill tone={d.outcome === 'accepted' ? 'accent' : d.outcome === 'rejected' ? 'danger' : 'warn'}>{d.outcome}</Pill>
                                    <span>{d.kind}</span>
                                </span>
                            } title={d.title}>
                                {d.subject && (
                                    <div className="text-[11px] font-mono uppercase tracking-wider mb-1" style={{ color: 'var(--fg-3)' }}>
                                        Subject: <span style={{ color: 'var(--fg-1)' }}>{d.subject}</span>
                                    </div>
                                )}
                                {d.rationale && (
                                    <p className="text-xs mt-1 leading-relaxed" style={{ color: 'var(--fg-1)' }}>{d.rationale}</p>
                                )}
                                <div className="mt-3 text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                                    {d.created_by} · {d.created_at}
                                    {d.audit_anchor && <> · anchor {d.audit_anchor.slice(0, 12)}</>}
                                </div>
                            </Card>
                        ))}
                    </section>
                )}
            </div>
        </AppLayout>
    );
}

function FormRow({ label, children }: { label: string; children: React.ReactNode }) {
    return (
        <label className="block">
            <span className="text-[10px] font-mono uppercase tracking-wider mb-1 block" style={{ color: 'var(--fg-3)' }}>{label}</span>
            {children}
        </label>
    );
}
