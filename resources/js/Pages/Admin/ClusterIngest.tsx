import type { JSX } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

/**
 * /admin/cluster-ingest — Phase A/B/C/D ingestion observability.
 *
 * Surfaces:
 *   - KPI tiles: runs / files / bytes / collars / curves / passages / embed%
 *   - Recent Phase A ingest runs (bronze.ingest_runs)
 *   - Top clusters by file count (bronze.ingest_manifest)
 *   - Per-project state: collars + curves + passages + embedding %
 *
 * Doc-phase 183.
 */

interface KPIs {
    total_ingest_runs: number;
    total_files_indexed: number;
    total_bytes_indexed: number;
    total_collars: number;
    total_well_log_curves: number;
    total_passages: number;
    passages_embedded: number;
    passages_pending_embed: number;
}

interface IngestRun {
    run_id: string;
    source_path: string;
    status: string;
    started_at: string;
    completed_at: string | null;
    files_seen: number;
    files_indexed: number;
    bytes_seen: number;
    summary: Record<string, unknown>;
}

interface ClusterRow {
    cluster_key: string;
    file_count: number;
    total_bytes: number;
}

interface ProjectState {
    project_id: string;
    project_name: string;
    slug: string;
    commodity?: string | null;
    region?: string | null;
    collar_count: number;
    curve_count: number;
    passage_count: number;
    embedded_count: number;
    embedding_pct: number;
}

interface PageProps {
    kpis: KPIs;
    recent_runs: IngestRun[];
    top_clusters: ClusterRow[];
    per_project: ProjectState[];
}

function bytesPretty(n: number): string {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let v = n;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i++;
    }
    return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}

function formatDate(iso: string | null): string {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleString();
    } catch {
        return iso;
    }
}

function statusBadge(status: string): JSX.Element {
    const map: Record<string, string> = {
        completed: 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300',
        running: 'border-sky-500/40 bg-sky-500/15 text-sky-300',
        failed: 'border-red-500/40 bg-red-500/15 text-red-300',
        cancelled: 'border-stone-700 bg-stone-800/40 text-stone-400',
    };
    const cls = map[status] ?? map.cancelled;
    return (
        <span className={`rounded border px-2 py-0.5 font-mono text-[10px] ${cls}`}>
            {status}
        </span>
    );
}

export default function ClusterIngest({
    kpis,
    recent_runs,
    top_clusters,
    per_project,
}: PageProps): JSX.Element {
    const peakClusterCount = top_clusters.reduce(
        (max, c) => (c.file_count > max ? c.file_count : max),
        0,
    );

    // Phase 2 real-time push — commit_ingestion_run (Dagster terminal asset)
    // broadcasts to admin.cluster-ingest at every materialization. KPIs +
    // recent runs + per-project state refresh; top_clusters omitted because
    // bronze.ingest_manifest cluster_key counts move on a different cadence
    // (slower, dominated by file-walk runs).
    useAdminSurfaceUpdated('cluster-ingest', null, () => {
        router.reload({ only: ['kpis', 'recent_runs', 'per_project'] });
    });

    return (
        <AppLayout>
            <Head title="Cluster Ingest — Admin" />
            <div className="min-h-screen bg-stone-950 text-stone-100">
                <div
                    className="mx-auto max-w-7xl px-6 py-8"
                    data-testid="cluster-ingest"
                >
                    <Link
                        href="/dashboard"
                        className="mb-4 inline-block text-sm text-stone-400 hover:text-amber-300"
                    >
                        ← Back to dashboard
                    </Link>

                    <header className="mb-6">
                        <h1 className="text-2xl font-semibold text-stone-50">
                            Cluster Ingest
                        </h1>
                        <p className="mt-1 text-sm text-stone-400">
                            Phase A/B/C/D ingestion state: large-archive walks,
                            silver layer population, KG sync, and Qdrant
                            embeddings.
                        </p>
                    </header>

                    {/* KPI tiles */}
                    <section className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-4">
                        <Tile label="Phase A walks" value={String(kpis.total_ingest_runs)} />
                        <Tile label="Files indexed" value={kpis.total_files_indexed.toLocaleString()} />
                        <Tile label="Bytes inspected" value={bytesPretty(kpis.total_bytes_indexed)} />
                        <Tile label="Silver collars" value={kpis.total_collars.toLocaleString()} tone="good" />
                        <Tile label="Well-log curves" value={kpis.total_well_log_curves.toLocaleString()} tone="good" />
                        <Tile label="Document passages" value={kpis.total_passages.toLocaleString()} />
                        <Tile
                            label="Passages embedded"
                            value={`${kpis.passages_embedded} / ${kpis.total_passages}`}
                            tone={
                                kpis.passages_embedded === kpis.total_passages
                                    ? 'good'
                                    : kpis.passages_pending_embed > 0
                                    ? 'bad'
                                    : 'neutral'
                            }
                        />
                        <Tile label="Pending embed" value={String(kpis.passages_pending_embed)} />
                    </section>

                    {/* Per-project state */}
                    <section className="mb-6 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Per-project state
                        </h2>
                        <div className="overflow-x-auto">
                            <table className="w-full text-left text-sm" data-testid="per-project">
                                <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                    <tr>
                                        <th className="px-3 py-2">Project</th>
                                        <th className="px-3 py-2">Region</th>
                                        <th className="px-3 py-2 text-right">Collars</th>
                                        <th className="px-3 py-2 text-right">Curves</th>
                                        <th className="px-3 py-2 text-right">Passages</th>
                                        <th className="px-3 py-2 text-right">Embedded</th>
                                        <th className="px-3 py-2">Embed %</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {per_project.length === 0 && (
                                        <tr>
                                            <td colSpan={7} className="px-3 py-8 text-center text-stone-500">
                                                No projects ingested yet.
                                            </td>
                                        </tr>
                                    )}
                                    {per_project.map((p) => (
                                        <tr
                                            key={p.project_id}
                                            className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30"
                                        >
                                            <td className="px-3 py-2">
                                                <div className="text-stone-100">{p.project_name}</div>
                                                <div className="font-mono text-xs text-stone-500">
                                                    {p.slug}
                                                </div>
                                            </td>
                                            <td className="px-3 py-2 text-xs text-stone-400">
                                                {p.region || '—'}
                                            </td>
                                            <td className="px-3 py-2 text-right tabular-nums">
                                                {p.collar_count}
                                            </td>
                                            <td className="px-3 py-2 text-right tabular-nums">
                                                {p.curve_count}
                                            </td>
                                            <td className="px-3 py-2 text-right tabular-nums">
                                                {p.passage_count}
                                            </td>
                                            <td className="px-3 py-2 text-right tabular-nums">
                                                {p.embedded_count}
                                            </td>
                                            <td className="px-3 py-2">
                                                <div className="flex items-center gap-2 text-xs">
                                                    <div className="h-2 w-16 overflow-hidden rounded bg-stone-800">
                                                        <div
                                                            className={`h-full ${
                                                                p.embedding_pct >= 95
                                                                    ? 'bg-emerald-500/70'
                                                                    : p.embedding_pct >= 50
                                                                    ? 'bg-amber-500/70'
                                                                    : 'bg-red-500/70'
                                                            }`}
                                                            style={{ width: `${p.embedding_pct}%` }}
                                                        />
                                                    </div>
                                                    <span className="text-stone-300">{p.embedding_pct}%</span>
                                                </div>
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </section>

                    {/* Top clusters */}
                    <section className="mb-6 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Top inner-zip clusters (by file count)
                        </h2>
                        <div className="overflow-x-auto">
                            <table className="w-full text-left text-sm" data-testid="top-clusters">
                                <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                    <tr>
                                        <th className="px-3 py-2">Cluster key</th>
                                        <th className="px-3 py-2">Distribution</th>
                                        <th className="px-3 py-2 text-right">Files</th>
                                        <th className="px-3 py-2 text-right">Bytes</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {top_clusters.length === 0 && (
                                        <tr>
                                            <td colSpan={4} className="px-3 py-8 text-center text-stone-500">
                                                No ingest_manifest rows yet.
                                            </td>
                                        </tr>
                                    )}
                                    {top_clusters.map((c) => {
                                        const pct = peakClusterCount > 0
                                            ? (c.file_count / peakClusterCount) * 100
                                            : 0;
                                        return (
                                            <tr
                                                key={c.cluster_key}
                                                className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30"
                                            >
                                                <td className="px-3 py-2 font-mono text-xs text-stone-300">
                                                    {c.cluster_key.replace('innerzip::', '')}
                                                </td>
                                                <td className="px-3 py-2">
                                                    <div className="h-2 w-32 overflow-hidden rounded bg-stone-800">
                                                        <div
                                                            className="h-full bg-sky-500/70"
                                                            style={{ width: `${pct}%` }}
                                                        />
                                                    </div>
                                                </td>
                                                <td className="px-3 py-2 text-right tabular-nums">
                                                    {c.file_count.toLocaleString()}
                                                </td>
                                                <td className="px-3 py-2 text-right tabular-nums text-stone-400">
                                                    {bytesPretty(c.total_bytes)}
                                                </td>
                                            </tr>
                                        );
                                    })}
                                </tbody>
                            </table>
                        </div>
                    </section>

                    {/* Recent runs */}
                    <section className="mb-6 rounded border border-stone-800 bg-stone-900">
                        <h2 className="border-b border-stone-800 px-4 py-2 text-sm font-semibold text-stone-200">
                            Recent Phase A walks
                        </h2>
                        <div className="overflow-x-auto">
                            <table className="w-full text-left text-sm" data-testid="recent-runs">
                                <thead className="border-b border-stone-800 text-xs uppercase tracking-wide text-stone-400">
                                    <tr>
                                        <th className="px-3 py-2">Run</th>
                                        <th className="px-3 py-2">Source</th>
                                        <th className="px-3 py-2">Status</th>
                                        <th className="px-3 py-2 text-right">Files</th>
                                        <th className="px-3 py-2 text-right">Bytes</th>
                                        <th className="px-3 py-2">Started</th>
                                        <th className="px-3 py-2">Completed</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {recent_runs.length === 0 && (
                                        <tr>
                                            <td colSpan={7} className="px-3 py-8 text-center text-stone-500">
                                                No Phase A walks have run.
                                            </td>
                                        </tr>
                                    )}
                                    {recent_runs.map((r) => (
                                        <tr
                                            key={r.run_id}
                                            className="border-b border-stone-800/60 last:border-b-0 hover:bg-stone-800/30"
                                        >
                                            <td className="px-3 py-2 font-mono text-xs text-stone-300">
                                                {r.run_id.slice(0, 8)}…
                                            </td>
                                            <td className="px-3 py-2 text-xs text-stone-400">
                                                {r.source_path.split('/').pop()}
                                            </td>
                                            <td className="px-3 py-2">{statusBadge(r.status)}</td>
                                            <td className="px-3 py-2 text-right tabular-nums">
                                                {r.files_indexed.toLocaleString()}
                                            </td>
                                            <td className="px-3 py-2 text-right tabular-nums text-stone-400">
                                                {bytesPretty(r.bytes_seen)}
                                            </td>
                                            <td className="px-3 py-2 text-xs text-stone-400">
                                                {formatDate(r.started_at)}
                                            </td>
                                            <td className="px-3 py-2 text-xs text-stone-400">
                                                {formatDate(r.completed_at)}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </section>

                    <footer className="mt-8 text-xs text-stone-500">
                        Read-only. Source tables:{' '}
                        <code className="text-stone-400">bronze.ingest_runs</code>,{' '}
                        <code className="text-stone-400">bronze.ingest_manifest</code>,{' '}
                        <code className="text-stone-400">silver.collars</code>,{' '}
                        <code className="text-stone-400">silver.well_log_curves</code>,{' '}
                        <code className="text-stone-400">silver.document_passages</code>.
                        Last loaded: {new Date().toLocaleString()}.
                    </footer>
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
