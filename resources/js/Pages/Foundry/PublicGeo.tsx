import { useState } from 'react';
import { Head, Link } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, EmptyState } from '@/Components/Foundry/primitives';

interface Jurisdiction { code: string; name: string; sources: number; last_sync: string }
interface Layer { id: string; label: string; tier: number; group: string; locked?: boolean }

interface PublicGeoProps {
    jurisdictions: Jurisdiction[];
    layers: Layer[];
    empty: boolean;
}

export default function FoundryPublicGeo({ jurisdictions, layers, empty }: PublicGeoProps) {
    const [activeCode, setActiveCode] = useState<string | null>(jurisdictions[0]?.code ?? null);
    const [layersOn, setLayersOn] = useState<Record<string, boolean>>({});

    const groups: Record<string, Layer[]> = layers.reduce((acc, l) => {
        (acc[l.group] = acc[l.group] || []).push(l);
        return acc;
    }, {} as Record<string, Layer[]>);

    return (
        <AppLayout>
            <Head title="Public Geoscience" />

            <div className="flex-1 grid grid-cols-[240px_1fr_300px] overflow-hidden" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                {/* Jurisdiction rail */}
                <aside className="border-r overflow-y-auto" style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}>
                    <div className="px-3 py-3 border-b text-[10px] font-mono uppercase tracking-[0.12em]" style={{ borderColor: 'var(--line-1)', color: 'var(--fg-3)' }}>
                        Jurisdictions · {jurisdictions.length}
                    </div>
                    {empty ? (
                        <div className="px-3 py-4 text-xs" style={{ color: 'var(--fg-3)' }}>None seeded.</div>
                    ) : jurisdictions.map((j) => (
                        <button
                            key={j.code}
                            type="button"
                            onClick={() => setActiveCode(j.code)}
                            className="w-full text-left px-3 py-2 border-b text-xs"
                            style={{
                                borderColor: 'var(--line-1)',
                                background: j.code === activeCode ? 'var(--accent-bg)' : 'transparent',
                            }}
                        >
                            <div className="flex items-center gap-2">
                                <span className="font-mono" style={{ color: 'var(--fg-3)' }}>{j.code}</span>
                                <span style={{ color: 'var(--fg-0)' }}>{j.name}</span>
                            </div>
                            <div className="text-[10px] font-mono uppercase tracking-wider mt-0.5" style={{ color: 'var(--fg-3)' }}>
                                {j.sources} sources · {j.last_sync.slice(0, 10)}
                            </div>
                        </button>
                    ))}
                </aside>

                {/* Map canvas */}
                <section className="flex flex-col overflow-hidden">
                    <PageHeader
                        eyebrow="PUBLIC GEOSCIENCE"
                        title="Read-only second corpus"
                        sub={activeCode ? `Active: ${activeCode}` : 'Select a jurisdiction'}
                    />
                    <div className="flex-1 overflow-y-auto p-6">
                        {empty ? (
                            <EmptyState title="No jurisdictions seeded." detail="Wyoming WSGS layers aren't yet ingested for US workspaces. Canadian jurisdictions load via CanadaJurisdictionsSeeder." />
                        ) : (
                            <Card eyebrow="MAP" title={`Layers for ${activeCode ?? '—'}`}>
                                <div className="h-96 rounded-md flex items-center justify-center" style={{ background: 'var(--bg-2)', border: '1px solid var(--line-1)' }}>
                                    <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                                        MapLibre map renders with selected layers. PGEO overlays served from Martin tile proxy.
                                    </div>
                                </div>
                            </Card>
                        )}
                    </div>
                </section>

                {/* Layer panel */}
                <aside className="border-l overflow-y-auto" style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}>
                    <div className="px-3 py-3 border-b text-[10px] font-mono uppercase tracking-[0.12em]" style={{ borderColor: 'var(--line-1)', color: 'var(--fg-3)' }}>
                        Layers · {layers.length}
                    </div>
                    {Object.entries(groups).map(([group, items]) => (
                        <div key={group} className="border-b pb-2" style={{ borderColor: 'var(--line-1)' }}>
                            <div className="px-3 pt-2 pb-1 text-[10px] font-mono uppercase tracking-[0.12em]" style={{ color: 'var(--fg-3)' }}>{group}</div>
                            {items.map((l) => (
                                <label key={l.id} className="flex items-center gap-2 px-3 py-1 text-xs cursor-pointer">
                                    {l.locked ? (
                                        <span style={{ color: 'var(--fg-3)' }}>🔒</span>
                                    ) : (
                                        <input type="checkbox" checked={layersOn[l.id] ?? false} onChange={(e) => setLayersOn({ ...layersOn, [l.id]: e.target.checked })} />
                                    )}
                                    <span style={{ color: l.locked ? 'var(--fg-3)' : 'var(--fg-1)' }}>{l.label}</span>
                                    {l.tier === 3 && <Pill tone="warn">T3</Pill>}
                                </label>
                            ))}
                        </div>
                    ))}
                    <div className="px-3 py-3">
                        <Link href="/public-geoscience/tier3-unlock" className="block text-center text-[10px] font-mono uppercase tracking-wider px-3 py-1.5 rounded border" style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}>
                            Request Tier 3 access
                        </Link>
                    </div>
                </aside>
            </div>
        </AppLayout>
    );
}
