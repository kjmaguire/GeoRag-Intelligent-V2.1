import { useEffect, useMemo, useState } from 'react';
import { router } from '@inertiajs/react';

/**
 * Foundry CommandPalette — ⌘K / Ctrl+K fuzzy nav.
 *
 * Items are static + Inertia shared-props-hydrated. Slash commands map to
 * chat actions (handled at the Chat page level when /command is dispatched).
 */

interface PaletteItem {
    kind: 'nav' | 'cmd';
    icon?: string;
    title: string;
    sub: string;
    href?: string;
    cmd?: string;
    group: string;
}

const ITEMS: PaletteItem[] = [
    { kind: 'nav', title: 'Portfolio', sub: 'Org dashboard', href: '/dashboard', group: 'Navigate' },
    { kind: 'nav', title: 'Projects', sub: 'Project picker', href: '/projects', group: 'Navigate' },
    { kind: 'nav', title: 'Chat threads', sub: 'Saved conversations', href: '/threads', group: 'Navigate' },
    { kind: 'nav', title: 'Inbox', sub: 'Mentions + reviews + refusals', href: '/inbox', group: 'Navigate' },
    { kind: 'nav', title: 'Public Geoscience', sub: 'Read-only second corpus', href: '/public-geoscience', group: 'Navigate' },
    { kind: 'nav', title: 'New project', sub: '4-step wizard', href: '/foundry/projects/new', group: 'Navigate' },
    { kind: 'nav', title: 'Workspace settings', sub: '10-section shell', href: '/settings', group: 'Navigate' },
    { kind: 'nav', title: 'Support cockpit (admin)', sub: '§10 admin replay', href: '/support-cockpit', group: 'Navigate' },
    { kind: 'cmd', title: '/compare', sub: 'Compare two holes or analogs', cmd: '/compare', group: 'Commands' },
    { kind: 'cmd', title: '/analog', sub: 'Find nearest-match analogs', cmd: '/analog', group: 'Commands' },
    { kind: 'cmd', title: '/permit', sub: 'Check consultation status', cmd: '/permit', group: 'Commands' },
    { kind: 'cmd', title: '/pin', sub: 'Pin answer as investigation', cmd: '/pin', group: 'Commands' },
    { kind: 'cmd', title: '/map', sub: 'Ask about current map viewport', cmd: '/map', group: 'Commands' },
    { kind: 'cmd', title: '/branch', sub: 'Fork this thread', cmd: '/branch', group: 'Commands' },
];

export default function CommandPalette() {
    const [open, setOpen] = useState(false);
    const [q, setQ] = useState('');
    const [cursor, setCursor] = useState(0);

    useEffect(() => {
        function onKey(e: KeyboardEvent) {
            if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
                e.preventDefault();
                setOpen((v) => !v);
            }
            if (e.key === 'Escape') setOpen(false);
        }
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, []);

    const filtered = useMemo(() => {
        const qq = q.trim().toLowerCase();
        if (!qq) return ITEMS;
        return ITEMS.filter((i) => `${i.title} ${i.sub}`.toLowerCase().includes(qq));
    }, [q]);

    function pick(item: PaletteItem) {
        setOpen(false);
        setQ('');
        if (item.href) router.visit(item.href);
        else if (item.cmd) router.visit(`/chat?prompt=${encodeURIComponent(item.cmd + ' ')}`);
    }

    function onKey(e: React.KeyboardEvent<HTMLInputElement>) {
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            setCursor((c) => Math.min(filtered.length - 1, c + 1));
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            setCursor((c) => Math.max(0, c - 1));
        } else if (e.key === 'Enter' && filtered[cursor]) {
            e.preventDefault();
            pick(filtered[cursor]);
        }
    }

    if (!open) return null;

    const grouped = filtered.reduce((acc, i) => {
        (acc[i.group] = acc[i.group] || []).push(i);
        return acc;
    }, {} as Record<string, PaletteItem[]>);

    let runningIdx = 0;

    return (
        <div className="fixed inset-0 z-[200] flex items-start justify-center pt-24 foundry" style={{ background: 'rgba(8,10,14,0.78)', backdropFilter: 'blur(4px)' }} onClick={() => setOpen(false)}>
            <div className="w-[560px] max-w-[94vw] rounded-md border overflow-hidden flex flex-col" style={{ background: 'var(--bg-0)', borderColor: 'var(--line-2)', boxShadow: '0 24px 60px rgba(0,0,0,0.5)' }} onClick={(e) => e.stopPropagation()}>
                <div className="flex items-center gap-2 px-3 py-2 border-b" style={{ borderColor: 'var(--line-1)' }}>
                    <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>⌘K</span>
                    <input
                        type="text"
                        autoFocus
                        value={q}
                        onChange={(e) => { setQ(e.target.value); setCursor(0); }}
                        onKeyDown={onKey}
                        placeholder="Search nav, projects, slash commands…"
                        className="flex-1 text-sm bg-transparent outline-none"
                        style={{ color: 'var(--fg-0)' }}
                    />
                </div>
                <div className="max-h-96 overflow-y-auto">
                    {Object.entries(grouped).map(([group, items]) => (
                        <div key={group}>
                            <div className="px-3 pt-2 pb-1 text-[10px] font-mono uppercase tracking-[0.12em]" style={{ color: 'var(--fg-3)' }}>{group}</div>
                            {items.map((i) => {
                                const isActive = runningIdx === cursor;
                                runningIdx++;
                                return (
                                    <button
                                        key={`${group}-${i.title}`}
                                        type="button"
                                        onClick={() => pick(i)}
                                        className="w-full text-left px-3 py-2 flex items-center gap-3"
                                        style={{
                                            background: isActive ? 'var(--accent-bg)' : 'transparent',
                                            color: isActive ? 'var(--fg-0)' : 'var(--fg-1)',
                                        }}
                                    >
                                        <span className="font-mono text-[10px] uppercase tracking-wider w-12" style={{ color: 'var(--fg-3)' }}>{i.kind}</span>
                                        <div className="flex-1">
                                            <div className="text-xs font-medium">{i.title}</div>
                                            <div className="text-[10px] font-mono" style={{ color: 'var(--fg-3)' }}>{i.sub}</div>
                                        </div>
                                    </button>
                                );
                            })}
                        </div>
                    ))}
                    {filtered.length === 0 && (
                        <div className="px-3 py-6 text-center text-xs" style={{ color: 'var(--fg-3)' }}>No matches.</div>
                    )}
                </div>
                <div className="px-3 py-1.5 border-t text-[10px] font-mono uppercase tracking-wider flex justify-between" style={{ borderColor: 'var(--line-1)', color: 'var(--fg-3)' }}>
                    <span>↑↓ navigate · ⏎ select</span>
                    <span>esc to close</span>
                </div>
            </div>
        </div>
    );
}
