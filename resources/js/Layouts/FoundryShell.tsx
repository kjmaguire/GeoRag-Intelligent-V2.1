import { useEffect, useMemo, useState } from 'react';
import { Link, usePage, router } from '@inertiajs/react';
import ProjectSelector from '../Components/ProjectSelector';
import CommandPalette from '../Components/Foundry/CommandPalette';
import type { PageProps } from '@/types';

/**
 * FoundryShell — the app's persistent chrome.
 *
 * Three tiers of navigation:
 *   1. ORG bar (top, always)
 *        LEFT:  Brand · Portfolio · Projects · Public Geo · + New
 *        RIGHT: Search · Review · Inbox · User menu · Theme
 *   2. PROJECT sub-bar (horizontal, only when in-project)
 *        Overview · Workspace · Chat · Reasoning · Targets · Data · Reader · Reports · Audit
 *   3. PROJECT left rail (vertical, only when in-project)
 *        Same nav as sub-bar, plus chat-threads + saved-views lists below.
 *
 * Chat lives INSIDE projects only (/projects/{slug}/chat) — no standalone /chat.
 * Admin surfaces live behind the user menu, not in the main top bar.
 */

interface FoundryShellProps {
    children: React.ReactNode;
    onProjectChange?: (projectId: string) => void;
}

const ORG_NAV: Array<{ id: string; href: string; label: string }> = [
    { id: 'portfolio', href: '/dashboard', label: 'Portfolio' },
    { id: 'projects', href: '/projects', label: 'Projects' },
    { id: 'publicgeo', href: '/foundry/public-geoscience', label: 'Public Geo' },
];

const PROJECT_NAV: Array<{ id: string; suffix: string; label: string; icon: string }> = [
    { id: 'overview', suffix: '', label: 'Overview', icon: 'home' },
    { id: 'workspace', suffix: '/workspace', label: 'Workspace', icon: 'map' },
    { id: 'chat', suffix: '/chat', label: 'Chat', icon: 'chat' },
    { id: 'reasoning', suffix: '/reasoning', label: 'Reasoning', icon: 'flask' },
    { id: 'targets', suffix: '/targets', label: 'Targets', icon: 'zap' },
    { id: 'data', suffix: '/sources', label: 'Data', icon: 'db' },
    // Live per-file ingestion progress for this project. Sibling of Data so
    // users see "what I uploaded" (Data) vs "where is it in the pipeline"
    // (Ingestion Runs) side by side. Surface owned by IngestionRunsController.
    { id: 'ingestion-runs', suffix: '/ingestion-runs', label: 'Ingestion Runs', icon: 'pulse' },
    // Lakehouse — bronze/silver/gold inventory of every ingested layer for
    // this project. Direct-URL entrypoint to per-hole DrillholeDetail
    // (§5.12) lives inside the Lakehouse + Workspace pages.
    { id: 'lakehouse', suffix: '/lakehouse', label: 'Lakehouse', icon: 'db' },
    { id: 'reader', suffix: '/corpus', label: 'Reader', icon: 'doc' },
    { id: 'reports', suffix: '/reports', label: 'Reports', icon: 'report' },
    // CC-01 Item 1 Slice 4 — Silver Review Queue surface for drill data
    // (lithology / assays / collars pending review). Sibling of Data so
    // geologists see "Data" (already accepted) vs "Review" (pending) at a glance.
    { id: 'drill-review', suffix: '/drill-review', label: 'Review', icon: 'shield' },
    { id: 'audit', suffix: '/audit', label: 'Audit', icon: 'shield' },
];

const ADMIN_NAV: Array<{ href: string; label: string }> = [
    { href: '/support-cockpit', label: 'Support Cockpit' },
    { href: '/admin/eval-dashboard', label: 'Eval Dashboard' },
    { href: '/admin/decision-history', label: 'Decision History' },
    { href: '/admin/hypothesis-workspace', label: 'Hypothesis Workspace' },
    { href: '/admin/cluster-ingest', label: 'Cluster Ingest' },
];

interface SharedRailData {
    project_threads?: Array<{ id: string; title: string; updated: string }>;
    project_saved_views?: Array<{ id: string; name: string; scope: string }>;
    inbox_count?: number;
    review_count?: number;
}

function Icon({ name, size = 12 }: { name: string; size?: number }) {
    const paths: Record<string, React.ReactNode> = {
        home: <path d="M3 11 12 4l9 7v9h-6v-6h-6v6H3z" />,
        map: <path d="M9 4 3 6v14l6-2m0-14 6 2m-6-2v14m6-12 6-2v14l-6 2m0-14v14" />,
        chat: <path d="M4 5h16v11H9l-5 4V5z" />,
        flask: <path d="M9 3h6 M10 3v6l-5 9 1 2h12l1-2-5-9V3" />,
        zap: <path d="M13 3 4 14h7l-1 7 9-11h-7z" />,
        db: (
            <>
                <ellipse cx="12" cy="5" rx="8" ry="2.5" />
                <path d="M4 5v6c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5V5" />
                <path d="M4 11v6c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5v-6" />
            </>
        ),
        doc: <path d="M7 3h8l4 4v14H7z M15 3v4h4" />,
        report: <path d="M6 3h12v18H6z M9 8h6 M9 12h6 M9 16h4" />,
        shield: <path d="M12 3 L20 7 L20 12 C20 16 16 20 12 22 C8 20 4 16 4 12 L4 7 Z" />,
        search: <path d="M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14zM20 20l-4-4" />,
        inbox: <path d="M4 13h5l1 3h4l1-3h5 M4 5h16v14H4z" />,
        review: <path d="M5 12l4 4 10-10" />,
        plus: <path d="M12 5v14 M5 12h14" />,
        bookmark: <path d="M6 4h12v17l-6-4-6 4z" />,
        chevron: <path d="M9 6 L15 12 L9 18" />,
        bell: <path d="M6 14v-3a6 6 0 0 1 12 0v3l1.5 3h-15z M10 20a2 2 0 0 0 4 0" />,
        pulse: <path d="M3 12h4l2-7 4 14 2-7h6" />,
    };
    return (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            {paths[name] ?? paths.home}
        </svg>
    );
}

function UserMenu() {
    const { auth } = usePage<PageProps>().props;
    const user = auth?.user ?? null;
    const [open, setOpen] = useState(false);
    const isAdmin = Boolean(auth?.user?.is_admin);

    function handleLogout() {
        const csrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
        fetch('/api/v1/auth/logout', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { Accept: 'application/json', ...(csrf ? { 'X-CSRF-TOKEN': csrf } : {}) },
        }).catch(() => {});
        try { localStorage.removeItem('georag_user'); } catch { /* */ }
        router.visit('/foundry/login');
    }

    if (!user) {
        return (
            <Link href="/foundry/login" className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border border-[var(--line-1)] text-[var(--fg-3)] hover:text-[var(--fg-0)] hover:border-[var(--line-2)]">
                Sign in
            </Link>
        );
    }

    const initials = user.name.split(' ').map((s) => s[0]).slice(0, 2).join('').toUpperCase();

    return (
        <div className="relative">
            <button
                type="button"
                onClick={() => setOpen((v) => !v)}
                onBlur={(e) => { if (!e.currentTarget.parentElement?.contains(e.relatedTarget as Node)) setOpen(false); }}
                className="flex items-center gap-2 px-1.5 py-1 rounded hover:bg-[var(--bg-2)]"
                aria-haspopup="true"
                aria-expanded={open}
            >
                <span className="w-6 h-6 rounded text-[10px] font-mono flex items-center justify-center" style={{ background: 'var(--accent-bg)', color: 'var(--accent)' }}>
                    {initials}
                </span>
                <span className="text-[11px] font-mono text-[var(--fg-2)] hidden sm:inline" title={user.email}>{user.name}</span>
            </button>
            {open && (
                <div className="absolute right-0 top-full mt-1 w-56 rounded border z-50" style={{ background: 'var(--bg-1)', borderColor: 'var(--line-2)', boxShadow: '0 16px 40px rgba(0,0,0,0.45)' }} role="menu">
                    <div className="px-3 py-2 border-b text-[10px] font-mono uppercase tracking-wider" style={{ borderColor: 'var(--line-1)', color: 'var(--fg-3)' }}>
                        Signed in as
                    </div>
                    <div className="px-3 py-2 border-b" style={{ borderColor: 'var(--line-1)' }}>
                        <div className="text-xs" style={{ color: 'var(--fg-0)' }}>{user.name}</div>
                        <div className="text-[10px] font-mono" style={{ color: 'var(--fg-3)' }}>{user.email}</div>
                    </div>
                    <Link href="/settings" onClick={() => setOpen(false)} role="menuitem" className="block px-3 py-2 text-xs hover:bg-[var(--bg-2)]" style={{ color: 'var(--fg-1)' }}>
                        Workspace settings
                    </Link>
                    {isAdmin && (
                        <>
                            <div className="px-3 pt-2 pb-1 text-[10px] font-mono uppercase tracking-wider border-t" style={{ borderColor: 'var(--line-1)', color: 'var(--fg-3)' }}>Admin</div>
                            {ADMIN_NAV.map((a) => (
                                <Link key={a.href} href={a.href} onClick={() => setOpen(false)} role="menuitem" className="block px-3 py-1.5 text-[11px] hover:bg-[var(--bg-2)]" style={{ color: 'var(--fg-1)' }}>
                                    {a.label}
                                </Link>
                            ))}
                        </>
                    )}
                    <button
                        type="button"
                        onClick={handleLogout}
                        role="menuitem"
                        className="w-full text-left px-3 py-2 text-xs border-t hover:bg-[var(--bg-2)]"
                        style={{ borderColor: 'var(--line-1)', color: 'var(--danger)' }}
                    >
                        Sign out
                    </button>
                </div>
            )}
        </div>
    );
}

function orgNavClass(active: boolean) {
    return [
        'text-[11px] font-mono uppercase tracking-wider px-2.5 py-1 rounded transition-colors',
        active
            ? 'text-[var(--accent)] bg-[var(--accent-bg)] border border-[var(--accent-dim)]'
            : 'text-[var(--fg-2)] hover:text-[var(--fg-0)] border border-transparent hover:bg-[var(--bg-2)]',
    ].join(' ');
}

function projectSubBarClass(active: boolean) {
    return [
        'text-[11px] font-mono uppercase tracking-wider px-3 py-1.5 transition-colors border-b-2',
        active
            ? 'text-[var(--accent)] border-[var(--accent)]'
            : 'text-[var(--fg-2)] hover:text-[var(--fg-0)] border-transparent',
    ].join(' ');
}

function projectRailClass(active: boolean) {
    return [
        'flex items-center gap-2 px-3 py-2 text-[11px] font-mono uppercase tracking-wider transition-colors',
        active
            ? 'text-[var(--accent)] bg-[var(--accent-bg)] border-l-2 border-[var(--accent)]'
            : 'text-[var(--fg-2)] hover:text-[var(--fg-0)] border-l-2 border-transparent hover:bg-[var(--bg-2)]',
    ].join(' ');
}

export default function FoundryShell({ children, onProjectChange }: FoundryShellProps) {
    const { url, props } = usePage<PageProps & SharedRailData>();
    const railData = props as PageProps & SharedRailData;

    // Theme — persisted dark/light.
    const [theme, setTheme] = useState<'dark' | 'light'>(() => {
        if (typeof window === 'undefined') return 'dark';
        try { return (localStorage.getItem('georag-foundry-theme') as 'dark' | 'light') ?? 'dark'; } catch { return 'dark'; }
    });
    useEffect(() => { try { localStorage.setItem('georag-foundry-theme', theme); } catch { /* */ } }, [theme]);

    const [mobileOpen, setMobileOpen] = useState(false);

    // Project-scope detection — anything starting with /projects/{slug}.
    const projectMatch = url.match(/^\/projects\/([^\/?#]+)(\/[^?#]*)?/);
    const inProject = Boolean(projectMatch && projectMatch[1] !== 'new');
    const currentSlug = inProject ? projectMatch![1] : null;
    const currentSubpath = inProject ? (projectMatch![2] ?? '') : '';

    const orgActive = (href: string) => {
        if (href === '/dashboard') return url === '/dashboard';
        if (href === '/projects') return url === '/projects' || url.startsWith('/projects?');
        return url.startsWith(href);
    };

    // Threads + saved views shown in the rail, hydrated by controllers
    // that opt-in via Inertia shared props. Default to empty arrays so the
    // shell renders cleanly for pages that don't provide them.
    const threads = railData.project_threads ?? [];
    const savedViews = railData.project_saved_views ?? [];
    const inboxCount = railData.inbox_count ?? 0;
    const reviewCount = railData.review_count ?? 0;

    const rootClass = ['foundry', theme === 'light' ? 'light' : ''].filter(Boolean).join(' ');
    const layoutGrid = inProject
        ? 'grid grid-rows-[44px_36px_1fr] grid-cols-[260px_1fr]'
        : 'grid grid-rows-[44px_1fr] grid-cols-1';

    const projectNavMemo = useMemo(() => PROJECT_NAV, []);

    return (
        <div className={rootClass} style={{ height: '100vh', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
            <a
                href="#main-content"
                className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:bg-[var(--accent)] focus:text-[var(--bg-0)] focus:px-4 focus:py-2 focus:rounded focus:text-xs"
            >
                Skip to content
            </a>

            <div className={`flex-1 ${layoutGrid}`} style={{ minHeight: 0, overflow: 'hidden' }}>
                {/* ORG bar — spans both columns */}
                <header
                    className="h-11 flex items-center gap-3 px-4 border-b shrink-0 col-span-full"
                    style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)' }}
                >
                    {/* Brand */}
                    <Link href="/dashboard" className="flex items-center gap-2 select-none">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="text-[var(--accent)]">
                            <path d="M12 3 L21 8 L21 16 L12 21 L3 16 L3 8 Z" />
                            <path d="M12 3 L12 21 M3 8 L21 16 M21 8 L3 16" opacity="0.35" />
                        </svg>
                        <span className="text-[11px] font-mono font-semibold tracking-[0.12em] text-[var(--fg-0)]">GEORAG</span>
                    </Link>

                    {/* LEFT — Portfolio · Projects · Public Geo · + New */}
                    <nav className="hidden sm:flex items-center gap-1 ml-2" aria-label="Org navigation">
                        {ORG_NAV.map((n) => (
                            <Link key={n.id} href={n.href} className={orgNavClass(orgActive(n.href))}>
                                {n.label}
                            </Link>
                        ))}
                        <Link
                            href="/foundry/projects/new"
                            className="flex items-center gap-1 text-[11px] font-mono uppercase tracking-wider px-2.5 py-1 text-[var(--accent)] hover:bg-[var(--accent-bg)] rounded"
                        >
                            <Icon name="plus" size={10} /> New
                        </Link>
                    </nav>

                    {inProject && (
                        <div className="ml-3 hidden md:flex items-center gap-2 pl-3 border-l" style={{ borderColor: 'var(--line-1)' }}>
                            <ProjectSelector onProjectChange={onProjectChange} />
                        </div>
                    )}

                    <div className="flex-1" />

                    {/* RIGHT — Search · Review · Inbox */}
                    <button
                        type="button"
                        onClick={() => window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true }))}
                        className="flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border"
                        style={{ background: 'var(--bg-2)', borderColor: 'var(--line-1)', color: 'var(--fg-2)' }}
                        title="Search (⌘K / Ctrl+K)"
                    >
                        <Icon name="search" size={10} />
                        Search
                        <span className="ml-1 px-1 rounded text-[9px]" style={{ background: 'var(--bg-3)', color: 'var(--fg-3)' }}>⌘K</span>
                    </button>

                    <Link
                        href="/inbox?tab=reviews"
                        className="relative flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded"
                        style={{ color: 'var(--fg-2)' }}
                        title="Review queue"
                    >
                        <Icon name="review" size={11} />
                        Review
                        {reviewCount > 0 && (
                            <span className="text-[9px] px-1 rounded" style={{ background: 'var(--warn)', color: 'var(--bg-0)' }}>
                                {reviewCount}
                            </span>
                        )}
                    </Link>

                    <Link
                        href="/inbox"
                        className="relative flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded"
                        style={{ color: 'var(--fg-2)' }}
                        title="Inbox"
                    >
                        <Icon name="inbox" size={11} />
                        Inbox
                        {inboxCount > 0 && (
                            <span className="text-[9px] px-1 rounded" style={{ background: 'var(--accent)', color: 'var(--bg-0)' }}>
                                {inboxCount}
                            </span>
                        )}
                    </Link>

                    {/* Theme toggle */}
                    <div className="flex p-0.5 rounded border" style={{ background: 'var(--bg-2)', borderColor: 'var(--line-1)' }}>
                        <button
                            type="button"
                            onClick={() => setTheme('dark')}
                            className={['px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider rounded', theme === 'dark' ? 'text-[var(--fg-0)]' : 'text-[var(--fg-3)]'].join(' ')}
                            style={{ background: theme === 'dark' ? 'var(--bg-3)' : 'transparent' }}
                            title="Dark"
                        >
                            D
                        </button>
                        <button
                            type="button"
                            onClick={() => setTheme('light')}
                            className={['px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider rounded', theme === 'light' ? 'text-[var(--fg-0)]' : 'text-[var(--fg-3)]'].join(' ')}
                            style={{ background: theme === 'light' ? 'var(--bg-3)' : 'transparent' }}
                            title="Light"
                        >
                            L
                        </button>
                    </div>

                    <UserMenu />

                    <button
                        type="button"
                        onClick={() => setMobileOpen(!mobileOpen)}
                        className="sm:hidden text-[var(--fg-3)] hover:text-[var(--fg-1)] p-1"
                        aria-label="Toggle navigation"
                    >
                        <svg width={18} height={18} viewBox="0 0 24 24" fill="currentColor">
                            <path d="M3 6h18 M3 12h18 M3 18h18" stroke="currentColor" strokeWidth="2" />
                        </svg>
                    </button>
                </header>

                {inProject && currentSlug && (
                    <>
                        {/* PROJECT sub-bar (horizontal) — spans both columns below the org bar */}
                        <div
                            className="h-9 flex items-stretch gap-1 px-4 border-b shrink-0 col-span-full overflow-x-auto"
                            style={{ background: 'var(--bg-0)', borderColor: 'var(--line-1)' }}
                        >
                            <div className="flex items-center text-[10px] font-mono uppercase tracking-widest pr-3 mr-1 border-r" style={{ color: 'var(--fg-3)', borderColor: 'var(--line-1)' }}>
                                Project
                            </div>
                            {projectNavMemo.map((n) => {
                                const href = `/projects/${currentSlug}${n.suffix}`;
                                const isActive = (n.suffix === '' && currentSubpath === '') || (n.suffix !== '' && currentSubpath.startsWith(n.suffix));
                                return (
                                    <Link key={n.id} href={href} className={projectSubBarClass(isActive)}>
                                        {n.label}
                                    </Link>
                                );
                            })}
                            <div className="flex-1" />
                            <div className="flex items-center text-[10px] font-mono uppercase tracking-widest" style={{ color: 'var(--fg-4)' }}>
                                {currentSlug}
                            </div>
                        </div>

                        {/* PROJECT left rail (vertical) — in row 3, column 1 */}
                        <aside
                            className="overflow-y-auto border-r"
                            style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)' }}
                            aria-label="Project navigation"
                        >
                            <nav className="py-2">
                                {projectNavMemo.map((n) => {
                                    const href = `/projects/${currentSlug}${n.suffix}`;
                                    const isActive = (n.suffix === '' && currentSubpath === '') || (n.suffix !== '' && currentSubpath.startsWith(n.suffix));
                                    return (
                                        <Link key={n.id} href={href} className={projectRailClass(isActive)}>
                                            <Icon name={n.icon} size={12} />
                                            <span>{n.label}</span>
                                        </Link>
                                    );
                                })}
                            </nav>

                            {/* Chat threads (project-scoped) */}
                            <div className="border-t mt-2 pt-3" style={{ borderColor: 'var(--line-1)' }}>
                                <div className="flex items-center justify-between px-3 pb-1.5">
                                    <span className="text-[10px] font-mono uppercase tracking-widest" style={{ color: 'var(--fg-3)' }}>
                                        Chat threads
                                    </span>
                                    <Link href={`/projects/${currentSlug}/chat`} className="text-[10px] font-mono" style={{ color: 'var(--accent)' }}>
                                        view all
                                    </Link>
                                </div>
                                {threads.length === 0 ? (
                                    <div className="px-3 py-1 text-[10px] font-mono" style={{ color: 'var(--fg-4)' }}>
                                        no threads yet
                                    </div>
                                ) : (
                                    threads.slice(0, 6).map((t) => (
                                        <Link
                                            key={t.id}
                                            href={`/projects/${currentSlug}/chat?thread=${t.id}`}
                                            className="block px-3 py-1.5 text-[11px] truncate"
                                            style={{ color: 'var(--fg-2)' }}
                                            title={t.title}
                                        >
                                            <Icon name="chat" size={9} />
                                            <span className="ml-2">{t.title}</span>
                                        </Link>
                                    ))
                                )}
                            </div>

                            {/* Saved views (project-scoped) */}
                            <div className="border-t mt-2 pt-3" style={{ borderColor: 'var(--line-1)' }}>
                                <div className="flex items-center justify-between px-3 pb-1.5">
                                    <span className="text-[10px] font-mono uppercase tracking-widest" style={{ color: 'var(--fg-3)' }}>
                                        Saved views
                                    </span>
                                    <Link href={`/projects/${currentSlug}/saved-views`} className="text-[10px] font-mono" style={{ color: 'var(--accent)' }}>
                                        view all
                                    </Link>
                                </div>
                                {savedViews.length === 0 ? (
                                    <div className="px-3 py-1 text-[10px] font-mono" style={{ color: 'var(--fg-4)' }}>
                                        no saved views
                                    </div>
                                ) : (
                                    savedViews.slice(0, 6).map((v) => (
                                        <button
                                            key={v.id}
                                            type="button"
                                            onClick={() => window.dispatchEvent(new CustomEvent('georag:map:restore', { detail: { view_id: v.id } }))}
                                            className="w-full text-left block px-3 py-1.5 text-[11px] truncate hover:bg-[var(--bg-2)]"
                                            style={{ color: 'var(--fg-2)' }}
                                            title={v.name}
                                        >
                                            <Icon name="bookmark" size={9} />
                                            <span className="ml-2">{v.name}</span>
                                            <span className="ml-2 text-[9px] uppercase" style={{ color: 'var(--fg-4)' }}>
                                                {v.scope}
                                            </span>
                                        </button>
                                    ))
                                )}
                            </div>
                        </aside>
                    </>
                )}

                {/* Mobile org-nav drawer */}
                {mobileOpen && (
                    <nav className="sm:hidden border-b px-3 py-2 flex flex-col gap-1 col-span-full" style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)' }} aria-label="Mobile navigation">
                        {ORG_NAV.map((n) => (
                            <Link key={n.id} href={n.href} className={orgNavClass(orgActive(n.href))} onClick={() => setMobileOpen(false)}>
                                {n.label}
                            </Link>
                        ))}
                    </nav>
                )}

                {/* Main content area — col-span-1 when inProject (so left rail takes col 1), col-span-full otherwise */}
                <main
                    id="main-content"
                    className={`flex flex-col overflow-hidden ${inProject ? '' : 'col-span-full'}`}
                    style={{ background: 'var(--bg-0)' }}
                >
                    {children}
                </main>
            </div>

            <CommandPalette />
        </div>
    );
}
