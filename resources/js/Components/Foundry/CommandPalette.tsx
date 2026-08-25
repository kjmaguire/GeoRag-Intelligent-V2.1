import { useEffect, useMemo, useState } from 'react';
import { router } from '@inertiajs/react';

/**
 * Foundry CommandPalette — ⌘K / Ctrl+K fuzzy nav.
 *
 * Items are static org-level nav plus project-scoped entries when the
 * shell is inside a project (FoundryShell passes the active slug).
 *
 * The old "/compare, /analog, …" command entries are gone: they routed to
 * a nonexistent standalone /chat (only /projects/{slug}/chat exists) with
 * a ?prompt= param nothing read, and no slash-command interceptor exists —
 * the text would have been sent verbatim as a RAG query. Dead on three
 * counts, so removed rather than rerouted.
 */

interface PaletteItem {
    kind: 'nav';
    title: string;
    sub: string;
    href: string;
    group: string;
}

const ORG_ITEMS: PaletteItem[] = [
    { kind: 'nav', title: 'Projects', sub: 'Project picker', href: '/projects', group: 'Navigate' },
    { kind: 'nav', title: 'New project', sub: '4-step wizard', href: '/foundry/projects/new', group: 'Navigate' },
    { kind: 'nav', title: 'Upload files', sub: 'Import wizard — PDF / TIFF / ZIP', href: '/foundry/imports/wizard', group: 'Navigate' },
];

export default function CommandPalette({ projectSlug = null }: { projectSlug?: string | null }) {
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

    const items = useMemo<PaletteItem[]>(() => {
        if (!projectSlug) return ORG_ITEMS;
        const base = `/projects/${projectSlug}`;
        return [
            ...ORG_ITEMS,
            { kind: 'nav', title: 'Overview', sub: 'Project overview', href: base, group: 'This project' },
            { kind: 'nav', title: 'Chat', sub: 'Ask the project corpus', href: `${base}/chat`, group: 'This project' },
            { kind: 'nav', title: 'Data', sub: 'Sources + lineage', href: `${base}/sources`, group: 'This project' },
            { kind: 'nav', title: 'Ingestion runs', sub: 'Live pipeline activity', href: `${base}/ingestion-runs`, group: 'This project' },
            // Reader (/corpus) and Quality (/imports/quality) merged into
            // Reports 2026-08-18; both paths still redirect there.
            { kind: 'nav', title: 'Reports', sub: 'Documents & ingest quality', href: `${base}/reports`, group: 'This project' },
            // Restored 2026-08-17 (reader-core trim reversal) — a real
            // page route (/projects/{slug}/workspace), unrelated to the
            // dead chat slash-commands described above.
            { kind: 'nav', title: 'Workspace', sub: 'Map, sections, 3D, logs', href: `${base}/workspace`, group: 'This project' },
            // Merged 2026-08-19 — Compare is a mode inside Workspace now, so
            // this deep-links straight to it rather than to the deleted
            // /compare page. Kept as its own palette entry because "compare"
            // is what a user types when they want it; it is not discoverable
            // by searching for "workspace".
            { kind: 'nav', title: 'Compare holes', sub: 'Side-by-side hole comparison', href: `${base}/workspace?mode=compare`, group: 'This project' },
            // Folded out of the project nav 2026-08-25 — Rasters is a
            // Workspace mode and Tables a Reports view. Both keep a palette
            // entry for the same reason Compare does: "rasters" is what a
            // user types when they want the raster catalogue, and it is not
            // discoverable by searching for "workspace".
            { kind: 'nav', title: 'Rasters', sub: 'Raster catalogue (Workspace mode)', href: `${base}/rasters`, group: 'This project' },
            { kind: 'nav', title: 'Tables', sub: 'Attribute tables (Reports view)', href: `${base}/attribute-tables`, group: 'This project' },
        ];
    }, [projectSlug]);

    const filtered = useMemo(() => {
        const qq = q.trim().toLowerCase();
        if (!qq) return items;
        return items.filter((i) => `${i.title} ${i.sub}`.toLowerCase().includes(qq));
    }, [q, items]);

    function pick(item: PaletteItem) {
        setOpen(false);
        setQ('');
        router.visit(item.href);
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
                        aria-label="Search navigation"
                        type="text"
                        autoFocus
                        value={q}
                        onChange={(e) => { setQ(e.target.value); setCursor(0); }}
                        onKeyDown={onKey}
                        placeholder="Search navigation…"
                        className="flex-1 text-sm bg-transparent outline-none"
                        style={{ color: 'var(--fg-0)' }}
                    />
                </div>
                <div className="max-h-96 overflow-y-auto">
                    {Object.entries(grouped).map(([group, groupItems]) => (
                        <div key={group}>
                            <div className="px-3 pt-2 pb-1 text-[10px] font-mono uppercase tracking-[0.12em]" style={{ color: 'var(--fg-3)' }}>{group}</div>
                            {groupItems.map((i) => {
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
