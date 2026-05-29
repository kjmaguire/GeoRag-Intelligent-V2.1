import { useState } from 'react';
import { Head, Link } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, Segmented } from '@/Components/Foundry/primitives';

interface SettingsProps {
    workspace: { id: string; name: string; slug: string; data_version: number };
    member_count: number;
    can_admin: boolean;
}

type Section = 'general' | 'members' | 'roles' | 'sso' | 'security' | 'tokens' | 'retention' | 'residency' | 'billing' | 'danger';

const SECTIONS: Array<{ id: Section; label: string; group: string }> = [
    { id: 'general', label: 'General', group: 'Workspace' },
    { id: 'members', label: 'Members', group: 'Workspace' },
    { id: 'roles', label: 'Roles & access', group: 'Workspace' },
    { id: 'sso', label: 'SSO / SAML', group: 'Security' },
    { id: 'security', label: 'Security', group: 'Security' },
    { id: 'tokens', label: 'API tokens', group: 'Security' },
    { id: 'retention', label: 'Data retention', group: 'Data' },
    { id: 'residency', label: 'Data residency', group: 'Data' },
    { id: 'billing', label: 'Plan & billing', group: 'Account' },
    { id: 'danger', label: 'Danger zone', group: 'Account' },
];

export default function FoundrySettings({ workspace, member_count, can_admin }: SettingsProps) {
    const [section, setSection] = useState<Section>('general');
    const [agentic, setAgentic] = useState(true);
    const [decisionMode, setDecisionMode] = useState<'modal' | 'silent'>('modal');
    const [anaphora, setAnaphora] = useState(true);
    const [whatChangedCadence, setWhatChangedCadence] = useState<'hourly' | 'daily' | 'weekly' | 'manual'>('daily');

    const grouped = SECTIONS.reduce((acc, s) => {
        (acc[s.group] = acc[s.group] || []).push(s);
        return acc;
    }, {} as Record<string, typeof SECTIONS>);

    return (
        <AppLayout>
            <Head title="Workspace Settings" />

            <div className="flex-1 grid grid-cols-[240px_1fr] overflow-hidden" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <aside className="border-r overflow-y-auto" style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}>
                    {Object.entries(grouped).map(([group, items]) => (
                        <div key={group}>
                            <div className="px-3 pt-4 pb-1 text-[10px] font-mono uppercase tracking-[0.14em]" style={{ color: 'var(--fg-3)' }}>{group}</div>
                            {items.map((s) => (
                                <button
                                    key={s.id}
                                    type="button"
                                    onClick={() => setSection(s.id)}
                                    className="w-full text-left px-3 py-2 text-xs transition-colors"
                                    style={{
                                        background: section === s.id ? 'var(--accent-bg)' : 'transparent',
                                        color: section === s.id ? 'var(--fg-0)' : 'var(--fg-2)',
                                    }}
                                >
                                    {s.label}
                                </button>
                            ))}
                        </div>
                    ))}
                </aside>

                <section className="overflow-y-auto">
                    <PageHeader
                        eyebrow="WORKSPACE SETTINGS"
                        title={workspace.name}
                        sub={`${workspace.slug} · v${workspace.data_version} · ${member_count} member${member_count === 1 ? '' : 's'}`}
                    />

                    <div className="px-8 py-6 space-y-4 max-w-3xl">
                        {section === 'general' && (
                            <>
                                <Card eyebrow="WORKSPACE" title="General">
                                    <FieldRow label="Name" value={workspace.name} />
                                    <FieldRow label="Slug" value={workspace.slug} />
                                    <FieldRow label="Data version" value={String(workspace.data_version)} />
                                </Card>
                                <Card eyebrow="AI FEATURES" title="§04j Agentic Retrieval · §9.9 Decisions · §9.13 What Changed">
                                    <FieldRow label="Agentic retrieval (§04j)">
                                        <input type="checkbox" checked={agentic} onChange={(e) => setAgentic(e.target.checked)} />
                                    </FieldRow>
                                    <FieldRow label="Anaphora resolution">
                                        <input type="checkbox" checked={anaphora} onChange={(e) => setAnaphora(e.target.checked)} />
                                    </FieldRow>
                                    <FieldRow label="Decision capture mode">
                                        <Segmented<'modal' | 'silent'>
                                            value={decisionMode}
                                            onChange={setDecisionMode}
                                            options={[
                                                { value: 'modal', label: 'Modal' },
                                                { value: 'silent', label: 'Silent' },
                                            ]}
                                        />
                                    </FieldRow>
                                    <FieldRow label="What Changed cadence">
                                        <Segmented<'hourly' | 'daily' | 'weekly' | 'manual'>
                                            value={whatChangedCadence}
                                            onChange={setWhatChangedCadence}
                                            options={[
                                                { value: 'hourly', label: 'Hourly' },
                                                { value: 'daily', label: 'Daily' },
                                                { value: 'weekly', label: 'Weekly' },
                                                { value: 'manual', label: 'Manual' },
                                            ]}
                                        />
                                    </FieldRow>
                                </Card>
                                {can_admin && (
                                    <Card eyebrow="SUPPORT · ADMIN TOOLS" title="§10 Support Cockpit">
                                        <Link href="/support-cockpit" className="text-[10px] font-mono uppercase tracking-wider px-3 py-1.5 rounded border inline-block" style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}>Open cockpit →</Link>
                                    </Card>
                                )}
                            </>
                        )}
                        {section === 'members' && <Card eyebrow="MEMBERS" title={`${member_count} member${member_count === 1 ? '' : 's'}`}><div className="text-xs" style={{ color: 'var(--fg-2)' }}>Member management UI deferred — invite by email + bulk CSV + roles assignment lands next.</div></Card>}
                        {section === 'roles' && <Card eyebrow="ROLES & ACCESS" title="RBAC"><div className="text-xs" style={{ color: 'var(--fg-2)' }}>Workspace roles: admin, qp, geologist, viewer. Per-project overrides + Tier-3 layer gating via /public-geoscience/tier3-unlock.</div></Card>}
                        {section === 'sso' && <Card eyebrow="SSO / SAML" title="Single Sign-On"><div className="text-xs" style={{ color: 'var(--fg-2)' }}>Providers: Okta · Azure · Google · OneLogin · Custom. SCIM provisioning available.</div></Card>}
                        {section === 'security' && <Card eyebrow="SECURITY" title="Authentication & access"><div className="text-xs" style={{ color: 'var(--fg-2)' }}>2FA · WebAuthn · password policy · IP allowlist · Tor block · geo restrictions.</div></Card>}
                        {section === 'tokens' && <Card eyebrow="API TOKENS" title="Personal access tokens"><div className="text-xs" style={{ color: 'var(--fg-2)' }}>Manage Sanctum personal access tokens for API + automation access.</div></Card>}
                        {section === 'retention' && <Card eyebrow="DATA RETENTION" title="Per-tier retention"><div className="text-xs" style={{ color: 'var(--fg-2)' }}>Bronze · Silver · Embeddings · Chat. NI 43-101 audit minimum 7y.</div></Card>}
                        {section === 'residency' && <Card eyebrow="DATA RESIDENCY" title="Region pinning"><div className="text-xs" style={{ color: 'var(--fg-2)' }}>Region pinning, LLM region lock, compliance posture grid.</div></Card>}
                        {section === 'billing' && <Card eyebrow="PLAN & BILLING" title="Pro plan"><div className="text-xs" style={{ color: 'var(--fg-2)' }}>Usage bars, cost breakdown, plan change. Stripe integration deferred.</div></Card>}
                        {section === 'danger' && <Card eyebrow="DANGER ZONE" title="Workspace lifecycle"><div className="text-xs" style={{ color: 'var(--danger)' }}>Export · Transfer ownership · Delete workspace. Each requires admin confirmation.</div></Card>}
                    </div>
                </section>
            </div>
        </AppLayout>
    );
}

function FieldRow({ label, value, children }: { label: string; value?: string; children?: React.ReactNode }) {
    return (
        <div className="grid grid-cols-[160px_1fr] gap-3 items-center py-2 border-b" style={{ borderColor: 'var(--line-1)' }}>
            <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>{label}</span>
            {children ?? <span className="text-xs" style={{ color: 'var(--fg-0)' }}>{value}</span>}
        </div>
    );
}
