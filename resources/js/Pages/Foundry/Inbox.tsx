import { useState } from 'react';
import { Head, router, usePage } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, EmptyState } from '@/Components/Foundry/primitives';
import { useUserInbox } from '@/Hooks/useUserInbox';

interface InboxItem {
    id: string;
    kind: 'mention' | 'review' | 'refusal' | string;
    title: string;
    detail: string;
    when: string;
}

interface InboxProps {
    mentions: InboxItem[];
    reviews: InboxItem[];
    refusals: InboxItem[];
    empty: boolean;
}

export default function FoundryInbox({ mentions, reviews, refusals, empty }: InboxProps) {
    const all = [...reviews, ...mentions, ...refusals];
    const [selectedId, setSelectedId] = useState<string | null>(all[0]?.id ?? null);
    const selected = all.find((i) => i.id === selectedId);

    // Phase 3 real-time push — subscribes to the Laravel default user
    // channel (App.Models.User.{userId}). The auth user is shared globally
    // via HandleInertiaRequests::share, so read it from usePage().
    //
    // Note (deferred): the three writers — silver.collaboration_mentions
    // inserts, silver.collaboration_review_requests inserts, and terminal
    // query-refusal writes — are not yet wired to POST to the bridge.
    // Phase 1.B of the §10v Team Collaboration spec ships the endpoints
    // that should fire post_user_inbox_updated; until then this listener
    // is plumbed but receives no events.
    const userId = (usePage().props as { auth?: { user?: { id?: number } } }).auth?.user?.id ?? null;
    useUserInbox(userId, () => {
        router.reload({ only: ['mentions', 'reviews', 'refusals', 'empty'] });
    });

    return (
        <AppLayout>
            <Head title="Inbox — GeoRAG" />

            <div className="flex-1 grid grid-cols-[400px_1fr] overflow-hidden" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <aside className="border-r overflow-y-auto flex flex-col" style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}>
                    <PageHeader
                        eyebrow="INBOX"
                        title={String(all.length)}
                        sub={`${reviews.length} reviews · ${mentions.length} mentions · ${refusals.length} refusals`}
                    />
                    {empty ? (
                        <div className="p-6"><EmptyState title="You're all caught up." /></div>
                    ) : (
                        all.map((it) => (
                            <button
                                key={it.id}
                                type="button"
                                onClick={() => setSelectedId(it.id)}
                                className="text-left px-4 py-3 border-b"
                                style={{
                                    borderColor: 'var(--line-1)',
                                    background: it.id === selectedId ? 'var(--accent-bg)' : 'transparent',
                                }}
                            >
                                <div className="flex items-center gap-2 mb-1">
                                    <Pill tone={it.kind === 'review' ? 'warn' : it.kind === 'refusal' ? 'danger' : 'info'} dot>{it.kind}</Pill>
                                    <span className="text-[10px] font-mono uppercase tracking-wider ml-auto" style={{ color: 'var(--fg-3)' }}>{it.when.slice(0, 16)}</span>
                                </div>
                                <div className="text-sm font-medium" style={{ color: 'var(--fg-0)' }}>{it.title}</div>
                                <div className="text-[11px] mt-0.5 line-clamp-2" style={{ color: 'var(--fg-2)' }}>{it.detail}</div>
                            </button>
                        ))
                    )}
                </aside>

                <section className="overflow-y-auto p-8">
                    {selected ? (
                        <Card eyebrow={selected.kind.toUpperCase()} title={selected.title}>
                            <div className="text-[11px] font-mono uppercase tracking-wider mb-3" style={{ color: 'var(--fg-3)' }}>
                                {selected.when}
                            </div>
                            <p className="text-sm leading-relaxed" style={{ color: 'var(--fg-1)' }}>{selected.detail}</p>
                        </Card>
                    ) : (
                        <EmptyState title="Select an item to view its details." />
                    )}
                </section>
            </div>
        </AppLayout>
    );
}
