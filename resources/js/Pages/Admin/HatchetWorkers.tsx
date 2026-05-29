import type { JSX } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

/**
 * /admin/hatchet-workers — Phase 1 Step 7 Hatchet Worker Dashboard.
 *
 * Reads engine state from the hatchet Postgres database (pgsql_hatchet
 * connection) and rolls it up into:
 *   - per-pool live / stale worker counts
 *   - registered workflow list (engine-side)
 *   - last-24h run rollup per workflow (succeeded/failed/running/queued + p50/p95)
 *
 * Read-only. The dashboard is a diagnostic surface during the Phase 1
 * shadow + cutover window; richer drilldown lives in Phase 10.
 */

interface Pool {
    name: string;
    live: number;
    stale: number;
    total_history: number;
    max_runs: number;
    last_heartbeat_at: string | null;
}

interface Workflow {
    name: string;
    version_count: number;
    latest_version_at: string | null;
}

interface RecentRuns {
    workflow_name: string;
    succeeded: number;
    failed: number;
    running: number;
    queued: number;
    cancelled: number;
    p50_duration_ms: number | null;
    p95_duration_ms: number | null;
    last_started_at: string | null;
}

interface EngineHealth {
    tenant_count: number;
    active_workflow_count: number;
    total_workers_24h: number;
    live_workers_now: number;
}

interface PageProps {
    pools: Pool[];
    workflows: Workflow[];
    recent_runs: RecentRuns[];
    engine_health: EngineHealth;
}

function formatDuration(ms: number | null): string {
    if (ms === null) return '—';
    if (ms < 1000) return `${ms} ms`;
    if (ms < 60_000) return `${(ms / 1000).toFixed(2)} s`;
    return `${(ms / 60_000).toFixed(1)} m`;
}

function formatDate(iso: string | null): string {
    if (iso === null) return '—';
    try {
        return new Date(iso).toLocaleString();
    } catch {
        return iso;
    }
}

function relativeAgo(iso: string | null): string {
    if (iso === null) return '—';
    try {
        const sec = (Date.now() - new Date(iso).getTime()) / 1000;
        if (sec < 60) return `${Math.round(sec)}s ago`;
        if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
        if (sec < 86_400) return `${Math.round(sec / 3600)}h ago`;
        return `${Math.round(sec / 86_400)}d ago`;
    } catch {
        return '—';
    }
}

export default function HatchetWorkers({
    pools,
    workflows,
    recent_runs,
    engine_health,
}: PageProps): JSX.Element {
    // Phase 2 real-time push — this dashboard rolls up the same
    // workflow runs that drive Admin/WorkflowRuns, so reusing the
    // admin.workflow-runs channel keeps the wiring minimal. Every
    // workflow completion refreshes the pool / recent_runs / health
    // tiles. (No separate `admin.hatchet-workers` channel — see plan.)
    useAdminSurfaceUpdated('workflow-runs', null, () => {
        router.reload({
            only: ['pools', 'workflows', 'recent_runs', 'engine_health'],
        });
    });

    return (
        <AppLayout>
            <Head title="Hatchet workers — Admin" />
            <div className="min-h-screen bg-stone-950 text-stone-100">
                <div className="mx-auto max-w-7xl px-6 py-8" data-testid="hatchet-workers-dashboard">
                    <Link href="/dashboard" className="mb-4 inline-block text-sm text-stone-400 hover:text-amber-300">
                        ← Back to dashboard
                    </Link>

                    <header className="mb-6">
                        <h1 className="text-2xl font-semibold text-stone-50">Hatchet workers</h1>
                        <p className="mt-1 text-sm text-stone-400">
                            Engine-side state from the <code className="text-stone-300">hatchet</code> database.
                            Workers are considered <em>live</em> when their heartbeat is within the last 90s.
                        </p>
                    </header>

                    {/* Engine health tiles */}
                    <section className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
                        <Tile label="Tenants" value={String(engine_health.tenant_count)} />
                        <Tile
                            label="Workflows registered"
                            value={String(engine_health.active_workflow_count)}
                        />
                        <Tile
                            label="Workers (24h)"
                            value={String(engine_health.total_workers_24h)}
                        />
                        <Tile
                            label="Live workers now"
                            value={String(engine_health.live_workers_now)}
                            tone={engine_health.live_workers_now > 0 ? 'good' : 'bad'}
                        />
                    </section>

                    {/* Pool rollup */}
                    <section className="mb-6 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Worker pools
                        </h2>
                        <div className="overflow-x-auto">
                            <table className="w-full text-left text-sm" data-testid="pool-table">
                                <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                    <tr>
                                        <th className="px-3 py-2">Pool</th>
                                        <th className="px-3 py-2 text-right">Live</th>
                                        <th className="px-3 py-2 text-right">Stale</th>
                                        <th className="px-3 py-2 text-right">History</th>
                                        <th className="px-3 py-2 text-right">Max runs / worker</th>
                                        <th className="px-3 py-2">Last heartbeat</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {pools.length === 0 && (
                                        <tr>
                                            <td colSpan={6} className="px-3 py-8 text-center text-stone-500">
                                                No workers have ever connected to this Hatchet engine.
                                            </td>
                                        </tr>
                                    )}
                                    {pools.map((p) => (
                                        <tr
                                            key={p.name}
                                            className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30"
                                        >
                                            <td className="px-3 py-2 font-mono text-xs text-stone-200">{p.name}</td>
                                            <td className="px-3 py-2 text-right">
                                                <span
                                                    className={`rounded border px-2 py-0.5 text-xs ${
                                                        p.live > 0
                                                            ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300'
                                                            : 'border-red-500/40 bg-red-500/15 text-red-300'
                                                    }`}
                                                >
                                                    {p.live}
                                                </span>
                                            </td>
                                            <td className="px-3 py-2 text-right text-stone-400">{p.stale}</td>
                                            <td className="px-3 py-2 text-right text-stone-500">{p.total_history}</td>
                                            <td className="px-3 py-2 text-right font-mono text-xs text-stone-300">
                                                {p.max_runs}
                                            </td>
                                            <td className="px-3 py-2 text-xs text-stone-300">
                                                {relativeAgo(p.last_heartbeat_at)}
                                                <span className="ml-2 text-stone-500" title={p.last_heartbeat_at ?? ''}>
                                                    {formatDate(p.last_heartbeat_at)}
                                                </span>
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </section>

                    {/* Last 24h workflow run rollup */}
                    <section className="mb-6 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Last 24h — workflow runs
                        </h2>
                        <div className="overflow-x-auto">
                            <table className="w-full text-left text-sm" data-testid="recent-runs-table">
                                <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                    <tr>
                                        <th className="px-3 py-2">Workflow</th>
                                        <th className="px-3 py-2 text-right">Succeeded</th>
                                        <th className="px-3 py-2 text-right">Failed</th>
                                        <th className="px-3 py-2 text-right">Running</th>
                                        <th className="px-3 py-2 text-right">Queued</th>
                                        <th className="px-3 py-2 text-right">Cancelled</th>
                                        <th className="px-3 py-2 text-right">p50</th>
                                        <th className="px-3 py-2 text-right">p95</th>
                                        <th className="px-3 py-2">Last started</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {recent_runs.length === 0 && (
                                        <tr>
                                            <td colSpan={9} className="px-3 py-8 text-center text-stone-500">
                                                No workflow runs in the last 24 hours.
                                            </td>
                                        </tr>
                                    )}
                                    {recent_runs.map((r) => (
                                        <tr
                                            key={r.workflow_name}
                                            className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30"
                                        >
                                            <td className="px-3 py-2 font-mono text-xs text-stone-200">
                                                {r.workflow_name}
                                            </td>
                                            <td className="px-3 py-2 text-right text-emerald-300">{r.succeeded}</td>
                                            <td
                                                className={`px-3 py-2 text-right ${
                                                    r.failed > 0 ? 'text-red-300' : 'text-stone-500'
                                                }`}
                                            >
                                                {r.failed}
                                            </td>
                                            <td className="px-3 py-2 text-right text-sky-300">{r.running}</td>
                                            <td className="px-3 py-2 text-right text-amber-300">{r.queued}</td>
                                            <td className="px-3 py-2 text-right text-stone-500">{r.cancelled}</td>
                                            <td className="px-3 py-2 text-right font-mono text-xs text-stone-300">
                                                {formatDuration(r.p50_duration_ms)}
                                            </td>
                                            <td className="px-3 py-2 text-right font-mono text-xs text-stone-300">
                                                {formatDuration(r.p95_duration_ms)}
                                            </td>
                                            <td className="px-3 py-2 text-xs text-stone-300">
                                                {relativeAgo(r.last_started_at)}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </section>

                    {/* Registered workflows */}
                    <section className="mb-6 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Registered workflows ({workflows.length})
                        </h2>
                        <div className="grid grid-cols-1 gap-2 px-4 py-3 md:grid-cols-2 lg:grid-cols-3">
                            {workflows.map((w) => (
                                <div
                                    key={w.name}
                                    className="flex items-baseline justify-between rounded border border-stone-800 bg-stone-800/30 px-3 py-2 text-xs"
                                >
                                    <div>
                                        <div className="font-mono text-stone-200">{w.name}</div>
                                        <div className="mt-0.5 text-stone-500">
                                            {w.version_count} version{w.version_count === 1 ? '' : 's'}
                                        </div>
                                    </div>
                                    <div className="text-stone-500">{relativeAgo(w.latest_version_at)}</div>
                                </div>
                            ))}
                        </div>
                    </section>
                </div>
            </div>
        </AppLayout>
    );
}

function Tile({
    label,
    value,
    tone = 'neutral',
}: {
    label: string;
    value: string;
    tone?: 'good' | 'bad' | 'neutral';
}): JSX.Element {
    const tones: Record<string, string> = {
        good: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300',
        bad: 'border-red-500/40 bg-red-500/10 text-red-300',
        neutral: 'border-stone-800 bg-stone-900 text-stone-100',
    };
    return (
        <div className={`rounded border p-3 ${tones[tone] ?? tones.neutral}`}>
            <div className="text-xs uppercase tracking-wide opacity-80">{label}</div>
            <div className="mt-1 text-2xl font-semibold">{value}</div>
        </div>
    );
}
