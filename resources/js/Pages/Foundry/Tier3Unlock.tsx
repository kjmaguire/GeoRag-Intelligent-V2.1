import { useState } from 'react';
import { Head, useForm } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill } from '@/Components/Foundry/primitives';
import type { Tier3UnlockProps } from '@/Types/Foundry';

/**
 * Foundry Tier3Unlock — request flow for jurisdiction-gated public
 * geoscience layers. Two-step: select layers → attest licensing terms.
 */
export default function FoundryTier3Unlock({ workspace_id, layers, request_status, can_approve }: Tier3UnlockProps) {
    const [step, setStep] = useState<0 | 1 | 2>(0);
    const [selected, setSelected] = useState<Record<string, boolean>>({});
    const [attestPurpose, setAttestPurpose] = useState(false);
    const [attestRetention, setAttestRetention] = useState(false);
    const [attestAttribution, setAttestAttribution] = useState(false);

    const { post, processing } = useForm({
        layer_ids: Object.keys(selected).filter((id) => selected[id]),
        attest_purpose: attestPurpose,
        attest_retention: attestRetention,
        attest_attribution: attestAttribution,
    });

    const allAttest = attestPurpose && attestRetention && attestAttribution;
    const anySelected = Object.values(selected).some(Boolean);

    function submit() {
        post('/public-geoscience/tier3-unlock', {
            preserveScroll: true,
            onSuccess: () => setStep(2),
        });
    }

    return (
        <AppLayout>
            <Head title="Tier 3 unlock — Public Geoscience" />

            <div className="flex-1 overflow-y-auto" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow="PUBLIC GEOSCIENCE · TIER 3 UNLOCK"
                    title="Request jurisdiction-gated layers"
                    sub={`Workspace ${workspace_id} · current status: ${request_status}`}
                />

                <div className="px-8 py-5 max-w-3xl">
                    <Stepper current={step} />

                    {step === 0 && (
                        <Card eyebrow="STEP 1" title="Select layers" className="mt-4">
                            <ul className="space-y-2">
                                {layers.map((l) => (
                                    <li key={l.layer_id} className="flex items-center gap-3 p-2 rounded" style={{ background: 'var(--bg-2)' }}>
                                        <input
                                            type="checkbox"
                                            checked={selected[l.layer_id] ?? false}
                                            onChange={(e) => setSelected({ ...selected, [l.layer_id]: e.target.checked })}
                                        />
                                        <div className="flex-1">
                                            <div className="text-xs font-medium" style={{ color: 'var(--fg-0)' }}>{l.label}</div>
                                            <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                                                {l.jurisdictions.join(' · ')} · {l.row_count_estimate}
                                            </div>
                                        </div>
                                        <Pill tone={l.license.startsWith('Restricted') ? 'warn' : 'info'}>{l.license}</Pill>
                                    </li>
                                ))}
                            </ul>
                            <div className="mt-4 flex justify-end">
                                <button
                                    type="button"
                                    onClick={() => setStep(1)}
                                    disabled={!anySelected}
                                    className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border disabled:opacity-40"
                                    style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                                >
                                    Next: Attestations →
                                </button>
                            </div>
                        </Card>
                    )}

                    {step === 1 && (
                        <Card eyebrow="STEP 2" title="Attest licensing terms" className="mt-4">
                            <Attestation
                                value={attestPurpose}
                                onChange={setAttestPurpose}
                                label="Purpose-of-use"
                                detail="I will use these layers only for exploration / research within this workspace."
                            />
                            <Attestation
                                value={attestRetention}
                                onChange={setAttestRetention}
                                label="Retention"
                                detail="I will not retain the underlying data beyond the workspace's data-retention policy."
                            />
                            <Attestation
                                value={attestAttribution}
                                onChange={setAttestAttribution}
                                label="Attribution"
                                detail="I will attribute the publishing jurisdiction in any output that derives from these layers."
                            />
                            <div className="mt-4 flex justify-between">
                                <button
                                    type="button"
                                    onClick={() => setStep(0)}
                                    className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                    style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                                >
                                    ← Back
                                </button>
                                <button
                                    type="button"
                                    onClick={submit}
                                    disabled={!allAttest || processing}
                                    className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border disabled:opacity-40"
                                    style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                                >
                                    {processing ? 'Submitting…' : 'Submit request'}
                                </button>
                            </div>
                        </Card>
                    )}

                    {step === 2 && (
                        <Card eyebrow="STEP 3" title="Request submitted" className="mt-4">
                            <p className="text-sm" style={{ color: 'var(--fg-1)' }}>
                                Your Tier 3 unlock request is pending admin review. You will receive a notification once it's approved or denied.
                            </p>
                            {can_approve && (
                                <p className="text-xs mt-3" style={{ color: 'var(--fg-3)' }}>
                                    (You have admin privileges — open the admin support cockpit to approve / deny requests.)
                                </p>
                            )}
                        </Card>
                    )}
                </div>
            </div>
        </AppLayout>
    );
}

function Attestation({ value, onChange, label, detail }: {
    value: boolean;
    onChange: (v: boolean) => void;
    label: string;
    detail: string;
}) {
    return (
        <label className="flex items-start gap-3 p-3 rounded mb-2 cursor-pointer" style={{ background: 'var(--bg-2)' }}>
            <input type="checkbox" checked={value} onChange={(e) => onChange(e.target.checked)} className="mt-0.5" />
            <div className="flex-1">
                <div className="text-xs font-medium" style={{ color: 'var(--fg-0)' }}>{label}</div>
                <div className="text-[11px] mt-0.5" style={{ color: 'var(--fg-2)' }}>{detail}</div>
            </div>
        </label>
    );
}

function Stepper({ current }: { current: number }) {
    const steps = ['Select layers', 'Attest', 'Submitted'];
    return (
        <ol className="flex items-center gap-2">
            {steps.map((s, i) => (
                <li key={i} className="flex items-center gap-2">
                    <span
                        className="w-6 h-6 rounded-full text-[11px] font-mono flex items-center justify-center"
                        style={{
                            background: i <= current ? 'var(--accent-bg)' : 'var(--bg-2)',
                            color: i <= current ? 'var(--accent)' : 'var(--fg-3)',
                            border: `1px solid ${i <= current ? 'var(--accent-dim)' : 'var(--line-1)'}`,
                        }}
                    >
                        {i + 1}
                    </span>
                    <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: i === current ? 'var(--fg-0)' : 'var(--fg-3)' }}>
                        {s}
                    </span>
                    {i < steps.length - 1 && <span style={{ color: 'var(--fg-3)' }}>→</span>}
                </li>
            ))}
        </ol>
    );
}
