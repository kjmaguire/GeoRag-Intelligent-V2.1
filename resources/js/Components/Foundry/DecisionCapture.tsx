import { useState } from 'react';
import { router } from '@inertiajs/react';
import { Modal, Card, Pill } from './primitives';

/**
 * DecisionCapture — §9.9–§9.10 facade with two variants.
 *   modal    — full friction; for "accept target" / "approve report" / "promote to silver"
 *   silent   — bottom-of-screen toast capturing the decision with one click
 */

export interface DecisionContext {
    kind: 'drill_target' | 'report_approved' | 'threshold_change' | 'source_promoted' | 'hypothesis_accept' | 'query_pin' | 'manual';
    subject: string | null;
    project_id: string | null;
    project_slug: string;
}

interface DecisionCaptureProps {
    ctx: DecisionContext | null;
    variant?: 'modal' | 'silent';
    onClose: () => void;
}

const KIND_LABELS: Record<DecisionContext['kind'], string> = {
    drill_target: 'Accept drill target',
    report_approved: 'Approve report for NI 43-101 pack',
    threshold_change: 'Change workspace threshold',
    source_promoted: 'Promote source to silver',
    hypothesis_accept: 'Accept hypothesis',
    query_pin: 'Pin retrieval as canonical',
    manual: 'Record decision',
};

export function DecisionCapture({ ctx, variant = 'modal', onClose }: DecisionCaptureProps) {
    if (!ctx) return null;
    if (variant === 'silent') {
        return <SilentToast ctx={ctx} onClose={onClose} />;
    }
    return <ModalCapture ctx={ctx} onClose={onClose} />;
}

function ModalCapture({ ctx, onClose }: { ctx: DecisionContext; onClose: () => void }) {
    const [title, setTitle] = useState(defaultTitle(ctx));
    const [rationale, setRationale] = useState('');
    const [outcome, setOutcome] = useState<'accepted' | 'rejected' | 'deferred'>('accepted');

    function commit() {
        router.post(`/projects/${ctx.project_slug}/decisions`, {
            title,
            kind: ctx.kind,
            subject: ctx.subject,
            rationale,
            outcome,
        }, { preserveScroll: true, onSuccess: onClose });
    }

    return (
        <Modal open={true} onClose={onClose} maxWidth={520} label="Record decision">
            <Card eyebrow="§9.9 DECISION" title={KIND_LABELS[ctx.kind]}>
                <div className="space-y-3">
                    <label className="block">
                        <span className="text-[10px] font-mono uppercase tracking-wider mb-1 block" style={{ color: 'var(--fg-3)' }}>Title</span>
                        <input type="text" value={title} onChange={(e) => setTitle(e.target.value)} className="w-full text-sm px-2 py-1.5 rounded border" style={{ background: 'var(--bg-2)', color: 'var(--fg-0)', borderColor: 'var(--line-2)' }} />
                    </label>
                    <label className="block">
                        <span className="text-[10px] font-mono uppercase tracking-wider mb-1 block" style={{ color: 'var(--fg-3)' }}>Rationale</span>
                        <textarea value={rationale} onChange={(e) => setRationale(e.target.value)} rows={4} className="w-full text-sm px-2 py-1.5 rounded border resize-none" style={{ background: 'var(--bg-2)', color: 'var(--fg-0)', borderColor: 'var(--line-2)' }} />
                    </label>
                    <label className="block">
                        <span className="text-[10px] font-mono uppercase tracking-wider mb-1 block" style={{ color: 'var(--fg-3)' }}>Outcome</span>
                        <div className="flex gap-1.5">
                            {(['accepted', 'rejected', 'deferred'] as const).map((o) => (
                                <button key={o} type="button" onClick={() => setOutcome(o)} className="text-[10px] font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                    style={{
                                        background: outcome === o ? 'var(--accent-bg)' : 'var(--bg-2)',
                                        color: outcome === o ? 'var(--accent)' : 'var(--fg-2)',
                                        borderColor: outcome === o ? 'var(--accent-dim)' : 'var(--line-2)',
                                    }}>
                                    {o}
                                </button>
                            ))}
                        </div>
                    </label>
                    <div className="flex justify-end gap-2">
                        <button type="button" onClick={onClose} className="text-[10px] font-mono uppercase tracking-wider px-3 py-1.5 rounded border" style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}>Cancel</button>
                        <button type="button" onClick={commit} className="text-[10px] font-mono uppercase tracking-wider px-3 py-1.5 rounded border" style={{ color: 'var(--bg-0)', background: 'var(--accent)', borderColor: 'var(--accent-dim)' }}>Commit decision</button>
                    </div>
                </div>
            </Card>
        </Modal>
    );
}

function SilentToast({ ctx, onClose }: { ctx: DecisionContext; onClose: () => void }) {
    function quickCommit(outcome: 'accepted' | 'rejected') {
        router.post(`/projects/${ctx.project_slug}/decisions`, {
            title: defaultTitle(ctx),
            kind: ctx.kind,
            subject: ctx.subject,
            rationale: '(silent capture — no rationale)',
            outcome,
        }, { preserveScroll: true, onSuccess: onClose });
    }

    return (
        <div className="fixed bottom-6 right-6 z-[120] foundry">
            <div className="rounded-md border p-3 flex items-center gap-3" style={{ background: 'var(--bg-1)', borderColor: 'var(--accent-dim)', boxShadow: '0 10px 30px rgba(0,0,0,0.5)' }}>
                <Pill tone="accent" dot>§9.9 silent</Pill>
                <div className="text-xs" style={{ color: 'var(--fg-1)' }}>
                    {KIND_LABELS[ctx.kind]}{ctx.subject && <> · <span className="font-mono" style={{ color: 'var(--fg-0)' }}>{ctx.subject}</span></>}
                </div>
                <button type="button" onClick={() => quickCommit('accepted')} className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border" style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}>Accept</button>
                <button type="button" onClick={() => quickCommit('rejected')} className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border" style={{ color: 'var(--warn)', borderColor: 'var(--line-2)' }}>Reject</button>
                <button type="button" onClick={onClose} className="text-[10px] font-mono uppercase tracking-wider px-2" style={{ color: 'var(--fg-3)' }}>×</button>
            </div>
        </div>
    );
}

function defaultTitle(ctx: DecisionContext): string {
    if (ctx.kind === 'drill_target' && ctx.subject) return `Accept ${ctx.subject} as priority drill target`;
    if (ctx.kind === 'report_approved' && ctx.subject) return `Approve ${ctx.subject} for NI 43-101 pack`;
    return KIND_LABELS[ctx.kind];
}
