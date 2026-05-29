import { Head, Link, router } from '@inertiajs/react';
import { useEffect, useState } from 'react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Stat, Card, Pill, EmptyState } from '@/Components/Foundry/primitives';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';

interface IngestSummary {
    in_flight: number;
    completed: number;
    latest_in_flight: string | null;
}

interface OverviewProps {
    project: {
        project_id: string;
        project_name: string;
        slug: string;
        region: string | null;
        commodity: string | null;
        status: string;
        crs_epsg: number | null;
        data_version: number;
    };
    kpis: Array<{ label: string; value: string; sub?: string; tone?: string }>;
    next_action: { title: string; detail: string; cta: string; href: string };
    recent_activity: Array<{ id: string; when: string; kind: 'query' | 'refusal' | string; text: string }>;
    ingest_summary: IngestSummary;
    empty: boolean;
}

export default function FoundryOverview({ project, kpis, next_action, recent_activity, ingest_summary, empty }: OverviewProps) {
    const [deleting, setDeleting] = useState(false);
    const [deleteError, setDeleteError] = useState<string | null>(null);
    const [ingest, setIngest] = useState<IngestSummary>(ingest_summary);

    // Poll the Ingestion Runs JSON endpoint while there is anything in flight,
    // so the "X files ingesting" tile updates without a hard refresh. Backs
    // off to a single poll-on-mount when nothing is in flight (no point
    // hitting the bucket on a quiet project).
    useEffect(() => {
        let cancelled = false;
        let timer: ReturnType<typeof setTimeout> | null = null;

        async function tick(): Promise<void> {
            try {
                const res = await fetch(`/projects/${project.slug}/ingestion-runs.json`, {
                    credentials: 'same-origin',
                    headers: { Accept: 'application/json' },
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const body = await res.json();
                if (cancelled) return;
                const totals = body.runs?.totals;
                const inFlightList = body.runs?.in_flight ?? [];
                setIngest({
                    in_flight: totals?.in_flight ?? 0,
                    completed: totals?.completed ?? 0,
                    latest_in_flight: inFlightList[0]?.filename ?? null,
                });
            } catch {
                // ignore — retry on next tick if still polling
            } finally {
                if (!cancelled && (ingest.in_flight > 0 || document.visibilityState === 'visible')) {
                    timer = setTimeout(tick, ingest.in_flight > 0 ? 5000 : 30000);
                }
            }
        }

        timer = setTimeout(tick, ingest.in_flight > 0 ? 5000 : 30000);

        return () => {
            cancelled = true;
            if (timer) clearTimeout(timer);
        };
    }, [project.slug, ingest.in_flight]);

    // Reliability spec Phase 2b — when an ingestion run completes AND
    // the post-completion MV refresh succeeds, refetch the Overview
    // Inertia props that depend on the freshened data (KPI counts, ingest
    // summary tile, recent activity). Partial reload only — no full SPA
    // navigation, no Vite re-bundle.
    useWorkspaceDataUpdated(project.project_id, () => {
        router.reload({
            only: ['project', 'kpis', 'next_action', 'recent_activity', 'ingest_summary'],
        });
    });

    async function handleDelete(): Promise<void> {
        const ok = window.confirm(
            `Delete project "${project.project_name}"? This permanently removes the project row from the database. This cannot be undone.`,
        );
        if (!ok) return;

        setDeleting(true);
        setDeleteError(null);
        try {
            const csrf =
                document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') ?? null;
            const headers: Record<string, string> = { Accept: 'application/json' };
            if (csrf) headers['X-CSRF-TOKEN'] = csrf;

            const res = await fetch(`/api/v1/projects/${project.project_id}`, {
                method: 'DELETE',
                credentials: 'same-origin',
                headers,
            });
            if (!res.ok && res.status !== 204) {
                const body = await res.json().catch(() => ({}));
                throw new Error(body.message || `HTTP ${res.status}`);
            }
            window.location.href = '/dashboard';
        } catch (err) {
            setDeleteError(err instanceof Error ? err.message : String(err));
            setDeleting(false);
        }
    }

    return (
        <AppLayout>
            <Head title={`${project.project_name} — Overview`} />

            <div className="flex-1 overflow-y-auto" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow="PROJECT · OVERVIEW"
                    title={project.project_name}
                    sub={
                        <span>
                            {project.region ?? '—'} · {project.commodity ?? '—'} · status <Pill tone={project.status === 'active' ? 'accent' : 'neutral'} dot>{project.status}</Pill>
                            {project.crs_epsg && <span> · EPSG:{project.crs_epsg}</span>}
                            <span> · v{project.data_version}</span>
                        </span>
                    }
                    actions={
                        <div className="flex items-center gap-2">
                            <Link
                                href={`/projects/${project.slug}/workspace`}
                                className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                            >
                                Open Workspace →
                            </Link>
                            <button
                                type="button"
                                onClick={handleDelete}
                                disabled={deleting}
                                className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border disabled:opacity-50"
                                style={{ color: '#fca5a5', background: 'rgba(127, 29, 29, 0.15)', borderColor: 'rgba(220, 38, 38, 0.4)' }}
                                title="Permanently delete this project from the database"
                            >
                                {deleting ? 'Deleting…' : 'Delete Project'}
                            </button>
                        </div>
                    }
                />

                {deleteError && (
                    <div className="mx-8 mt-4 px-3 py-2 text-xs rounded border border-red-800/50 bg-red-950/40 text-red-300">
                        Failed to delete project: {deleteError}
                    </div>
                )}

                {/* Ingestion banner — only renders when there is in-flight or
                    recently-completed activity. Polls the Ingestion Runs JSON
                    endpoint above; click navigates to the full page. */}
                {(ingest.in_flight > 0 || ingest.completed > 0) && (
                    <section className="px-8 pt-5 pb-3">
                        <Link
                            href={`/projects/${project.slug}/ingestion-runs`}
                            className="block rounded border transition-colors hover:bg-[var(--bg-hover)]"
                            style={{
                                background: ingest.in_flight > 0 ? 'var(--accent-bg)' : 'var(--bg-2)',
                                borderColor: ingest.in_flight > 0 ? 'var(--accent-dim)' : 'var(--line-1)',
                            }}
                        >
                            <div className="flex items-center gap-4 px-4 py-3">
                                <div className="flex items-center gap-2">
                                    {ingest.in_flight > 0 && (
                                        <span
                                            className="inline-block h-2 w-2 rounded-full"
                                            style={{ background: 'var(--accent)', animation: 'pulse 2s ease-in-out infinite' }}
                                        />
                                    )}
                                    <span className="text-[10px] font-mono uppercase tracking-[0.12em]" style={{ color: 'var(--fg-3)' }}>
                                        Ingestion
                                    </span>
                                </div>
                                <div className="flex-1 text-sm" style={{ color: 'var(--fg-0)' }}>
                                    {ingest.in_flight > 0 ? (
                                        <>
                                            <span style={{ color: 'var(--accent)' }}>
                                                {ingest.in_flight} file{ingest.in_flight === 1 ? '' : 's'} ingesting
                                            </span>
                                            {ingest.latest_in_flight && (
                                                <span className="ml-2 text-xs" style={{ color: 'var(--fg-2)' }}>
                                                    · latest: {ingest.latest_in_flight}
                                                </span>
                                            )}
                                        </>
                                    ) : (
                                        <span>
                                            {ingest.completed} document{ingest.completed === 1 ? '' : 's'} ingested · pipeline idle
                                        </span>
                                    )}
                                </div>
                                <span className="text-xs font-mono uppercase tracking-wider" style={{ color: 'var(--fg-2)' }}>
                                    Open runs →
                                </span>
                            </div>
                        </Link>
                    </section>
                )}

                {/* KPI strip */}
                <section className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-px px-8 py-5" style={{ background: 'var(--line-1)' }}>
                    {kpis.map((k, i) => (
                        <Stat key={i} label={k.label} value={k.value} sub={k.sub} tone={k.tone as 'accent' | 'warn' | 'neutral' | undefined} />
                    ))}
                </section>

                <section className="px-8 py-6 grid grid-cols-1 lg:grid-cols-[1.4fr_1fr] gap-5">
                    {/* Next-action card */}
                    <Card eyebrow="NEXT ACTION" title={next_action.title}>
                        <p className="text-sm leading-relaxed mb-3" style={{ color: 'var(--fg-1)' }}>{next_action.detail}</p>
                        <Link
                            href={next_action.href}
                            className="inline-block text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                            style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                        >
                            {next_action.cta} →
                        </Link>

                        {/* Quick links grid */}
                        <div className="mt-6 pt-4 border-t" style={{ borderColor: 'var(--line-1)' }}>
                            <div className="text-[10px] font-mono uppercase tracking-[0.12em] mb-3" style={{ color: 'var(--fg-3)' }}>Jump to surface</div>
                            <div className="grid grid-cols-3 gap-2">
                                {[
                                    { label: 'Workspace', href: `/projects/${project.slug}/workspace`, sub: 'Map · Section · 3D · Structure · Logs' },
                                    { label: 'Chat', href: `/projects/${project.slug}/chat`, sub: 'Threaded reasoning' },
                                    { label: 'Reasoning', href: `/projects/${project.slug}/reasoning`, sub: '4-stage workbench' },
                                    { label: 'Targets', href: `/projects/${project.slug}/targets`, sub: '§8 drill recs' },
                                    { label: 'Data', href: `/projects/${project.slug}/sources`, sub: 'Sources + lineage' },
                                    { label: 'Audit', href: `/projects/${project.slug}/audit`, sub: 'NI 43-101 ledger' },
                                ].map((q) => (
                                    <Link
                                        key={q.label}
                                        href={q.href}
                                        className="block p-3 rounded border transition-colors hover:bg-[var(--bg-hover)]"
                                        style={{ background: 'var(--bg-2)', borderColor: 'var(--line-1)' }}
                                    >
                                        <div className="text-xs font-medium" style={{ color: 'var(--fg-0)' }}>{q.label}</div>
                                        <div className="text-[10px] font-mono mt-0.5" style={{ color: 'var(--fg-3)' }}>{q.sub}</div>
                                    </Link>
                                ))}
                            </div>
                        </div>
                    </Card>

                    {/* Activity feed */}
                    <Card eyebrow="RECENT ACTIVITY" title={`${recent_activity.length} events`} padded={false}>
                        {recent_activity.length === 0 ? (
                            <div className="px-4 py-8 text-center text-xs" style={{ color: 'var(--fg-3)' }}>
                                No activity in this project yet.
                            </div>
                        ) : (
                            recent_activity.map((a, i) => (
                                <div
                                    key={a.id}
                                    className="px-4 py-2.5 grid grid-cols-[60px_1fr] gap-3 border-b"
                                    style={{ borderColor: i === recent_activity.length - 1 ? 'transparent' : 'var(--line-1)' }}
                                >
                                    <span className="text-[10px] font-mono" style={{ color: 'var(--fg-3)' }}>{a.when}</span>
                                    <div>
                                        <Pill tone={a.kind === 'refusal' ? 'warn' : 'info'} dot>{a.kind}</Pill>
                                        <div className="text-xs mt-1" style={{ color: 'var(--fg-1)' }}>
                                            {a.text || <em style={{ color: 'var(--fg-3)' }}>(no text)</em>}
                                        </div>
                                    </div>
                                </div>
                            ))
                        )}
                    </Card>
                </section>

                {empty && (
                    <section className="px-8 pb-8">
                        <EmptyState
                            title="Project waiting on data."
                            detail="Once collars/samples ingest and queries flow, the Overview will fill in automatically with KPIs, activity feed, and your next-best action."
                        />
                    </section>
                )}
            </div>
        </AppLayout>
    );
}
