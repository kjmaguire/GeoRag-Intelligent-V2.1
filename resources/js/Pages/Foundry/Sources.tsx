import { useState } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, StatusDot, EmptyState } from '@/Components/Foundry/primitives';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';

interface FileTypeRow {
    kind: string;
    count: number;
    bytes: number;
}

interface IngestRunRow {
    id: string;
    source_path: string;
    started_at: string;
    completed_at: string;
    status: string;
    files_seen: number;
    files_indexed: number;
    files_skipped: number;
    bytes_seen: number;
    error_text: string;
}

interface ParserActivityRow {
    parser: string;
    version: string;
    rows_written: number;
    last_run: string;
    tables_touched: number;
}

interface ReportRow {
    id: string;
    title: string;
    company: string;
    filing_date: string;
    commodity: string;
    version: number;
    created_at: string;
}

interface SourcesStats {
    sections: string[];
    total_files_in_project: number;
    total_bytes_in_project: number;
    reports_in_project: number;
    passages_in_project: number;
    collars_in_project: number;
    parsers_active: number;
    ingest_runs_in_project: number;
    avg_quality_score: number | null;
    low_confidence_pages: number;
    total_pages_reviewed: number;
}

interface SourcesProps {
    project: { project_id: string; project_name: string; slug: string };
    stats: SourcesStats;
    file_types: FileTypeRow[];
    recent_runs: IngestRunRow[];
    parser_activity: ParserActivityRow[];
    reports: ReportRow[];
    empty: boolean;
    scope_note: string;
}

type Tab = 'inventory' | 'parsers' | 'reports' | 'runs';

const TABS: Array<{ id: Tab; label: string }> = [
    { id: 'inventory', label: 'File inventory' },
    { id: 'parsers', label: 'Parsers & provenance' },
    { id: 'reports', label: 'Reports in project' },
    { id: 'runs', label: 'Ingestion runs' },
];

function humanBytes(n: number): string {
    if (n <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i += 1;
    }
    return `${v.toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2)} ${units[i]}`;
}

function shortDate(s: string | null | undefined): string {
    if (!s) return '—';
    return s.slice(0, 16).replace('T', ' ');
}

function statusTone(s: string): 'accent' | 'warn' | 'danger' | 'info' | 'neutral' {
    if (s === 'completed' || s === 'success' || s === 'ok') return 'accent';
    if (s === 'running' || s === 'pending') return 'info';
    if (s === 'failed' || s === 'error') return 'danger';
    if (s === 'partial' || s === 'needs-review') return 'warn';
    return 'neutral';
}

export default function FoundrySources({
    project,
    stats,
    file_types,
    recent_runs,
    parser_activity,
    reports,
    empty,
    scope_note,
}: SourcesProps) {
    const [tab, setTab] = useState<Tab>('inventory');

    // Phase 3 real-time push — ingest_pdf / drill-upload write silver.reports
    // (parser_used + status), which is the data source for parser_activity.
    // The 'reports' affected_type covers all flavors of parser activity drift.
    useWorkspaceDataUpdated(project.project_id, (event) => {
        if (event.affected_types.includes('reports')) {
            router.reload({ only: ['stats', 'file_types', 'recent_runs', 'parser_activity', 'reports', 'empty'] });
        }
    });

    return (
        <AppLayout>
            <Head title={`Data · ${project.project_name}`} />

            <div
                className="flex-1 overflow-y-auto"
                style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}
            >
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · DATA`}
                    title="Corpus, parsers, and ingestion lineage"
                    sub={
                        stats.sections.length > 0
                            ? `PLSS ${stats.sections.join(', ')} · ${stats.total_files_in_project.toLocaleString()} files · ${humanBytes(stats.total_bytes_in_project)} · ${stats.collars_in_project.toLocaleString()} collars`
                            : 'No bronze data ingested into this project yet.'
                    }
                    actions={
                        <div className="flex gap-2">
                            <Link
                                href={`/projects/${project.slug}/graph`}
                                className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                            >
                                Open graph →
                            </Link>
                            <Link
                                href={`/projects/${project.slug}/corpus`}
                                className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                style={{
                                    color: 'var(--accent)',
                                    background: 'var(--accent-bg)',
                                    borderColor: 'var(--accent-dim)',
                                }}
                            >
                                + Connect source
                            </Link>
                        </div>
                    }
                />

                {scope_note && (
                    <div
                        className="px-8 py-2 text-[10px] font-mono uppercase tracking-wider border-b"
                        style={{
                            background: 'var(--bg-1)',
                            color: 'var(--fg-3)',
                            borderColor: 'var(--line-1)',
                        }}
                    >
                        Scope · {scope_note}
                    </div>
                )}

                {empty ? (
                    <div className="px-8 py-12">
                        <EmptyState
                            title="No data ingested into this workspace yet."
                            detail="Drop a zip or folder into the Import Wizard and Bronze ingestion will start indexing files. As parsers run, silver rows appear here."
                        />
                    </div>
                ) : (
                    <>
                        {/* Stat strip — all project-scoped */}
                        <section className="px-8 py-4">
                            <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
                                <StatTile
                                    label="Files in project"
                                    value={stats.total_files_in_project.toLocaleString()}
                                    sub={
                                        stats.sections.length > 0
                                            ? `PLSS ${stats.sections.join(' / ')}`
                                            : 'no sections linked yet'
                                    }
                                />
                                <StatTile
                                    label="Volume"
                                    value={humanBytes(stats.total_bytes_in_project)}
                                    sub="bronze inventory"
                                />
                                <StatTile
                                    label="Reports"
                                    value={stats.reports_in_project.toLocaleString()}
                                    sub="silver.reports"
                                    tone="accent"
                                />
                                <StatTile
                                    label="Passages"
                                    value={stats.passages_in_project.toLocaleString()}
                                    sub="chunked into Qdrant"
                                />
                                <StatTile
                                    label="Parsers active"
                                    value={stats.parsers_active.toLocaleString()}
                                    sub={`${stats.ingest_runs_in_project.toLocaleString()} ingest runs`}
                                />
                                <StatTile
                                    label="Quality"
                                    value={
                                        typeof stats.avg_quality_score === 'number'
                                            ? stats.avg_quality_score.toFixed(2)
                                            : '—'
                                    }
                                    sub={
                                        stats.total_pages_reviewed > 0
                                            ? `${stats.low_confidence_pages}/${stats.total_pages_reviewed} low-conf pages`
                                            : 'not assessed yet'
                                    }
                                    tone={
                                        typeof stats.avg_quality_score === 'number' && stats.avg_quality_score >= 0.7
                                            ? 'accent'
                                            : stats.low_confidence_pages > 0
                                              ? 'warn'
                                              : 'neutral'
                                    }
                                />
                            </div>
                        </section>

                        {/* Tabs */}
                        <section className="px-8">
                            <div
                                className="flex items-center gap-1 border-b"
                                style={{ borderColor: 'var(--line-1)' }}
                            >
                                {TABS.map((t) => (
                                    <button
                                        key={t.id}
                                        type="button"
                                        onClick={() => setTab(t.id)}
                                        className="px-3 py-2 text-[11px] font-mono uppercase tracking-wider transition-colors"
                                        style={{
                                            color: tab === t.id ? 'var(--accent)' : 'var(--fg-2)',
                                            borderBottom:
                                                '2px solid ' +
                                                (tab === t.id ? 'var(--accent)' : 'transparent'),
                                        }}
                                    >
                                        {t.label}
                                    </button>
                                ))}
                            </div>
                        </section>

                        <section className="px-8 py-6">
                            {tab === 'inventory' && <FileInventoryTab rows={file_types} totalFiles={stats.total_files_in_project} totalBytes={stats.total_bytes_in_project} />}
                            {tab === 'parsers' && <ParsersTab rows={parser_activity} />}
                            {tab === 'reports' && <ReportsTab rows={reports} project={project} stats={stats} />}
                            {tab === 'runs' && <RunsTab rows={recent_runs} />}
                        </section>
                    </>
                )}
            </div>
        </AppLayout>
    );
}

// ── Tab: File inventory ───────────────────────────────────────────────
function FileInventoryTab({
    rows,
    totalFiles,
    totalBytes,
}: {
    rows: FileTypeRow[];
    totalFiles: number;
    totalBytes: number;
}) {
    return (
        <Card eyebrow="BRONZE · INGEST_MANIFEST" title="Files in this project, by type">
            {rows.length === 0 ? (
                <div className="text-xs" style={{ color: 'var(--fg-3)' }}>
                    No files indexed yet.
                </div>
            ) : (
                <div className="space-y-1">
                    {rows.map((r) => {
                        const pct = totalFiles > 0 ? (r.count / totalFiles) * 100 : 0;
                        return (
                            <div
                                key={r.kind}
                                className="grid grid-cols-[120px_1fr_100px_100px] gap-3 items-center px-3 py-2 border-b text-xs"
                                style={{ borderColor: 'var(--line-1)' }}
                            >
                                <div className="flex items-center gap-2">
                                    <Pill tone={kindTone(r.kind)}>{r.kind}</Pill>
                                </div>
                                <div className="relative h-3 rounded overflow-hidden" style={{ background: 'var(--bg-2)' }}>
                                    <div
                                        className="absolute inset-y-0 left-0"
                                        style={{
                                            width: `${Math.max(2, pct)}%`,
                                            background: 'var(--accent)',
                                            opacity: 0.7,
                                        }}
                                    />
                                </div>
                                <div className="font-mono text-right" style={{ color: 'var(--fg-1)' }}>
                                    {r.count.toLocaleString()}
                                </div>
                                <div className="font-mono text-right text-[11px]" style={{ color: 'var(--fg-3)' }}>
                                    {humanBytes(r.bytes)}
                                </div>
                            </div>
                        );
                    })}
                    <div
                        className="grid grid-cols-[120px_1fr_100px_100px] gap-3 items-center px-3 py-2 text-xs"
                        style={{ color: 'var(--fg-2)' }}
                    >
                        <div className="font-mono uppercase tracking-wider">Total</div>
                        <div />
                        <div className="font-mono text-right">{totalFiles.toLocaleString()}</div>
                        <div className="font-mono text-right">{humanBytes(totalBytes)}</div>
                    </div>
                </div>
            )}
        </Card>
    );
}

function kindTone(k: string): 'accent' | 'info' | 'warn' | 'neutral' {
    const t = k.toLowerCase();
    if (['pdf', 'xlsx', 'xls'].includes(t)) return 'accent';
    if (['las', 'log', 'segy', 'segd'].includes(t)) return 'info';
    if (['tiff', 'tif', 'jpeg', 'jpg', 'png'].includes(t)) return 'warn';
    return 'neutral';
}

// ── Tab: Parsers & provenance ─────────────────────────────────────────
function ParsersTab({ rows }: { rows: ParserActivityRow[] }) {
    return (
        <Card eyebrow="BRONZE · PROVENANCE" title="Parsers active on this project's silver rows">
            <div className="text-xs mb-3" style={{ color: 'var(--fg-2)' }}>
                Every row in <code>silver.collars</code> + <code>silver.reports</code> for this project
                joins back to a row in <code>bronze.provenance</code> tagged with the parser name that
                wrote it. Use this to see which ingestion paths produced the data you're querying.
            </div>
            {rows.length === 0 ? (
                <div className="text-xs" style={{ color: 'var(--fg-3)' }}>
                    No silver rows in this project carry provenance yet.
                </div>
            ) : (
                <Card padded={false}>
                    <div
                        className="grid grid-cols-[180px_90px_100px_100px_1fr] text-[10px] font-mono uppercase tracking-wider px-4 py-2 border-b"
                        style={{ color: 'var(--fg-3)', borderColor: 'var(--line-1)' }}
                    >
                        <div>Parser</div>
                        <div>Version</div>
                        <div className="text-right">Rows</div>
                        <div className="text-right">Tables</div>
                        <div>Last run</div>
                    </div>
                    {rows.map((p) => (
                        <div
                            key={p.parser + p.version}
                            className="grid grid-cols-[180px_90px_100px_100px_1fr] gap-2 items-center px-4 py-2 border-b text-xs"
                            style={{ borderColor: 'var(--line-1)' }}
                        >
                            <div style={{ color: 'var(--fg-0)' }}>
                                <code>{p.parser}</code>
                            </div>
                            <div className="font-mono text-[11px]" style={{ color: 'var(--fg-3)' }}>
                                {p.version}
                            </div>
                            <div className="font-mono text-right" style={{ color: 'var(--fg-1)' }}>
                                {p.rows_written.toLocaleString()}
                            </div>
                            <div className="font-mono text-right" style={{ color: 'var(--fg-2)' }}>
                                {p.tables_touched}
                            </div>
                            <div className="font-mono text-[11px]" style={{ color: 'var(--fg-2)' }}>
                                {shortDate(p.last_run)}
                            </div>
                        </div>
                    ))}
                </Card>
            )}
        </Card>
    );
}

// ── Tab: Reports in project ───────────────────────────────────────────
function ReportsTab({
    rows,
    project,
    stats,
}: {
    rows: ReportRow[];
    project: SourcesProps['project'];
    stats: SourcesStats;
}) {
    return (
        <Card
            eyebrow="SILVER · REPORTS"
            title={`${stats.reports_in_project.toLocaleString()} reports linked to ${project.project_name}`}
        >
            <div className="text-xs mb-3" style={{ color: 'var(--fg-2)' }}>
                Reports that were promoted from bronze into the RAG corpus and tagged with this
                project's <code>project_id</code>. Showing the {Math.min(rows.length, 30)} most recent.
            </div>
            {rows.length === 0 ? (
                <div className="text-xs" style={{ color: 'var(--fg-3)' }}>
                    No reports in this project yet.
                </div>
            ) : (
                <Card padded={false}>
                    <div
                        className="grid grid-cols-[1fr_180px_120px_90px_120px] text-[10px] font-mono uppercase tracking-wider px-4 py-2 border-b"
                        style={{ color: 'var(--fg-3)', borderColor: 'var(--line-1)' }}
                    >
                        <div>Title</div>
                        <div>Company</div>
                        <div>Filing</div>
                        <div className="text-right">v</div>
                        <div>Added</div>
                    </div>
                    {rows.map((r) => (
                        <div
                            key={r.id}
                            className="grid grid-cols-[1fr_180px_120px_90px_120px] gap-2 items-center px-4 py-2 border-b text-xs"
                            style={{ borderColor: 'var(--line-1)' }}
                        >
                            <div className="truncate" style={{ color: 'var(--fg-0)' }}>
                                {r.title}
                            </div>
                            <div className="font-mono text-[11px] truncate" style={{ color: 'var(--fg-2)' }}>
                                {r.company || '—'}
                            </div>
                            <div className="font-mono text-[11px]" style={{ color: 'var(--fg-2)' }}>
                                {r.filing_date ? r.filing_date.slice(0, 10) : '—'}
                            </div>
                            <div className="font-mono text-right" style={{ color: 'var(--fg-2)' }}>
                                {r.version}
                            </div>
                            <div className="font-mono text-[11px]" style={{ color: 'var(--fg-3)' }}>
                                {shortDate(r.created_at)}
                            </div>
                        </div>
                    ))}
                </Card>
            )}
        </Card>
    );
}

// ── Tab: Ingestion runs ───────────────────────────────────────────────
function RunsTab({ rows }: { rows: IngestRunRow[] }) {
    return (
        <Card eyebrow="BRONZE · INGEST_RUNS" title="Ingestion jobs that touched this project's sections">
            <div className="text-xs mb-3" style={{ color: 'var(--fg-2)' }}>
                Every Phase B walk through an archive (zip / folder / Hatchet trigger) writes a row
                here with the file counts and byte totals. Look for non-completed status as a signal
                that an archive is mid-flight or stalled.
            </div>
            {rows.length === 0 ? (
                <div className="text-xs" style={{ color: 'var(--fg-3)' }}>
                    No ingestion runs recorded yet.
                </div>
            ) : (
                <Card padded={false}>
                    <div
                        className="grid grid-cols-[1fr_110px_140px_80px_80px_80px] text-[10px] font-mono uppercase tracking-wider px-4 py-2 border-b"
                        style={{ color: 'var(--fg-3)', borderColor: 'var(--line-1)' }}
                    >
                        <div>Source</div>
                        <div>Status</div>
                        <div>Started</div>
                        <div className="text-right">Seen</div>
                        <div className="text-right">Indexed</div>
                        <div className="text-right">Skipped</div>
                    </div>
                    {rows.map((r) => (
                        <div
                            key={r.id}
                            className="grid grid-cols-[1fr_110px_140px_80px_80px_80px] gap-2 items-center px-4 py-2 border-b text-xs"
                            style={{ borderColor: 'var(--line-1)' }}
                        >
                            <div className="truncate" style={{ color: 'var(--fg-0)' }}>
                                <span className="font-mono text-[11px]">
                                    {r.source_path.split(/[\\/]/).slice(-2).join('/') || '—'}
                                </span>
                                {r.error_text && (
                                    <div
                                        className="text-[10px] truncate mt-0.5"
                                        style={{ color: 'var(--danger, #d65a5a)' }}
                                    >
                                        {r.error_text}
                                    </div>
                                )}
                            </div>
                            <div className="flex items-center gap-2">
                                <StatusDot status={r.status} />
                                <Pill tone={statusTone(r.status)}>{r.status}</Pill>
                            </div>
                            <div className="font-mono text-[11px]" style={{ color: 'var(--fg-2)' }}>
                                {shortDate(r.started_at)}
                            </div>
                            <div className="font-mono text-right" style={{ color: 'var(--fg-2)' }}>
                                {r.files_seen.toLocaleString()}
                            </div>
                            <div className="font-mono text-right" style={{ color: 'var(--accent)' }}>
                                {r.files_indexed.toLocaleString()}
                            </div>
                            <div
                                className="font-mono text-right"
                                style={{ color: r.files_skipped > 0 ? 'var(--warn, #d6a14a)' : 'var(--fg-3)' }}
                            >
                                {r.files_skipped.toLocaleString()}
                            </div>
                        </div>
                    ))}
                </Card>
            )}
        </Card>
    );
}

// ── Helpers ───────────────────────────────────────────────────────────
function StatTile({
    label,
    value,
    sub,
    tone,
}: {
    label: string;
    value: string;
    sub?: string;
    tone?: 'accent' | 'info' | 'warn' | 'neutral';
}) {
    const color =
        tone === 'accent'
            ? 'var(--accent)'
            : tone === 'warn'
              ? 'var(--warn, #d6a14a)'
              : tone === 'info'
                ? 'var(--info, #6aa7ff)'
                : 'var(--fg-0)';
    return (
        <div
            className="p-3 rounded-md border"
            style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)' }}
        >
            <div
                className="text-[10px] font-mono uppercase tracking-wider mb-1"
                style={{ color: 'var(--fg-3)' }}
            >
                {label}
            </div>
            <div className="text-xl font-mono" style={{ color }}>
                {value}
            </div>
            {sub && (
                <div className="text-[10px] font-mono mt-1" style={{ color: 'var(--fg-3)' }}>
                    {sub}
                </div>
            )}
        </div>
    );
}
