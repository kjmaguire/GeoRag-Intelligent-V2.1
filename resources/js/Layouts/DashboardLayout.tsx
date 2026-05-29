import { useState, useEffect } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import type { Project } from '@/types';
import type { ProjectRosterRow } from '@/Types/Dashboard';

interface DashboardLayoutProps {
    children: React.ReactNode;
    currentSlug?: string;
}

function UserChip() {
    let user: { name: string } | null = null;
    try {
        const stored = typeof window !== 'undefined' ? localStorage.getItem('georag_user') : null;
        user = stored ? JSON.parse(stored) : null;
    } catch {
        // Malformed localStorage value — ignore.
    }
    const initial = user?.name?.charAt(0)?.toUpperCase() ?? '?';

    return (
        <div
            className="w-8 h-8 rounded-full flex items-center justify-center text-[13px] font-semibold"
            style={{
                background: 'linear-gradient(135deg, var(--dashboard-copper-dim), var(--dashboard-copper))',
                color: 'var(--dashboard-bg)',
                fontFamily: 'var(--dashboard-serif)',
            }}
        >
            {initial}
        </div>
    );
}

export default function DashboardLayout({ children, currentSlug }: DashboardLayoutProps) {
    const [projects, setProjects] = useState<ProjectRosterRow[]>([]);

    useEffect(() => {
        let cancelled = false;
        async function fetchProjects() {
            try {
                const res = await fetch('/api/v1/dashboard/portfolio/projects', {
                    credentials: 'same-origin',
                    headers: {
                        Accept: 'application/json',
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                });
                if (!res.ok) return;
                const json = await res.json();
                if (!cancelled) setProjects(json.data ?? json);
            } catch {
                // Silently fail — picker will just show portfolio option.
            }
        }
        fetchProjects();
        return () => { cancelled = true; };
    }, []);

    function handlePickerChange(e: React.ChangeEvent<HTMLSelectElement>) {
        const val = e.target.value;
        if (val === 'portfolio') {
            router.visit('/dashboard');
        } else {
            router.visit(`/dashboard/projects/${val}`);
        }
    }

    const pickerValue = currentSlug ?? 'portfolio';

    return (
        <div className="dashboard min-h-screen" style={{ background: 'var(--dashboard-bg)', color: 'var(--dashboard-text)' }}>
            <Head>
                <link rel="preconnect" href="https://fonts.googleapis.com" />
                <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
                <link
                    href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,300;9..144,400;9..144,500;9..144,600&family=Geist:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap"
                    rel="stylesheet"
                />
            </Head>

            {/* Topbar */}
            <header
                className="sticky top-0 z-10 flex items-center gap-8 px-8 py-3.5"
                style={{
                    background: 'rgba(13, 15, 18, 0.85)',
                    backdropFilter: 'blur(12px)',
                    borderBottom: '1px solid var(--dashboard-line)',
                }}
            >
                <Link href="/dashboard" className="no-underline" style={{ textDecoration: 'none' }}>
                    <span
                        className="text-[22px] font-medium tracking-tight"
                        style={{ fontFamily: 'var(--dashboard-serif)', color: 'var(--dashboard-text)' }}
                    >
                        Geo<span style={{ color: 'var(--dashboard-copper)' }}>RAG</span>
                    </span>
                </Link>

                <div
                    className="text-[11px] uppercase tracking-wider"
                    style={{ fontFamily: 'var(--dashboard-mono)', color: 'var(--dashboard-text-mute)' }}
                >
                    <strong style={{ color: 'var(--dashboard-text-dim)', fontWeight: 500 }}>WORKSPACE</strong>
                    {' · Northern Cordillera Holdings · '}
                    <strong style={{ color: 'var(--dashboard-text-dim)', fontWeight: 500 }}>DASHBOARD</strong>
                </div>

                <div className="ml-auto flex items-center gap-3">
                    <span
                        className="text-[10px] uppercase tracking-widest"
                        style={{ fontFamily: 'var(--dashboard-mono)', color: 'var(--dashboard-text-mute)' }}
                    >
                        View
                    </span>
                    <select
                        value={pickerValue}
                        onChange={handlePickerChange}
                        className="min-w-[240px] cursor-pointer rounded px-3.5 py-2 text-[13px] transition-colors"
                        style={{
                            background: 'var(--dashboard-bg-elev)',
                            border: '1px solid var(--dashboard-line)',
                            color: 'var(--dashboard-text)',
                            fontFamily: 'var(--dashboard-sans)',
                        }}
                    >
                        <option value="portfolio">All projects (Portfolio)</option>
                        <option disabled>──────────────</option>
                        {projects.map((p) => (
                            <option key={p.id} value={p.slug}>
                                {p.name} — {p.region}
                            </option>
                        ))}
                    </select>
                    <UserChip />
                </div>
            </header>

            {/* Main content */}
            <main className="relative z-[1] mx-auto max-w-[1480px] px-8 py-8">
                {children}
            </main>
        </div>
    );
}
