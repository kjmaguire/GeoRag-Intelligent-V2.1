import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, Stat, EmptyState, Segmented } from '@/Components/Foundry/primitives';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';

interface AuditRow {
    id: string;
    run_id: string | null;
    created_at: string | null;
    user: string;
    query_text: string;
    query_class: string | null;
    model: string | null;
    tokens: number | null;
    latency_ms: number | null;
    status: 'ok' | 'refused' | 'error' | string;
    confidence: number | null;
    citation_count: number;
    refusal_reason: string | null;
}

interface AuditLogProps {
    project: { project_id: string; project_name: string; slug: string };
    totals: { queries: number; refused: number; avg_latency_ms: number; total_tokens: number };
    refusal_pct: number;
    rows: AuditRow[];
    filters: { status: string | null; days: number };
    empty: boolean;
}

/**
 * Foundry AuditLog — NI 43-101 query provenance ledger.
 *
 * Reads real audit.query_audit_log for the active project. Refusal-by-gate
 * breakdown + verification-proof export are wired through the existing
 * audit/chain_verify.py helper (Phase H4).
 */
export default function FoundryAuditLog({ project, totals, refusal_pct, rows, filters, empty }: AuditLogProps) {
    // Phase 3 real-time push — every QueryController::store dispatches
    // WorkspaceDataUpdated with affected_types=['audit_log']. The 2-second
    // debounce in the hook collapses high-frequency query bursts into one
    // partial reload (a chat session firing 5 queries in 2s = 1 reload, not 5).
    useWorkspaceDataUpdated(project.project_id, (event) => {
        if (event.affected_types.includes('audit_log')) {
            router.reload({ only: ['totals', 'refusal_pct', 'rows', 'empty'] });
        }
    });


    function setStatus(s: 'all' | 'ok' | 'refused' | 'error') {
        router.get(
            `/projects/${project.slug}/audit`,
            { status: s === 'all' ? undefined : s, days: filters.days },
            { preserveState: true, preserveScroll: true }
        );
    }

    function exportProof() {
        window.location.href = `/admin/audit-explorer/verify-chain?project_id=${project.project_id}&days=${filters.days}`;
    }

    return (
        <AppLayout>
            <Head title={`Audit · ${project.project_name}`} />

            <div className="flex-1 overflow-y-auto" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · AUDIT`}
                    title="NI 43-101 provenance"
                    sub={`Last ${filters.days} days · audit.query_audit_log`}
                    actions={
                        <button
                            type="button"
                            onClick={exportProof}
                            className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                            style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                        >
                            Export verification proof
                        </button>
                    }
                />

                <section className="grid grid-cols-2 sm:grid-cols-4 gap-px px-8 py-5" style={{ background: 'var(--line-1)' }}>
                    <Stat label="QUERIES" value={String(totals.queries)} sub={`${filters.days}d window`} tone="accent" />
                    <Stat label="REFUSAL RATE" value={`${refusal_pct}%`} sub={`${totals.refused} refused`} tone={refusal_pct >= 5 ? 'warn' : 'neutral'} />
                    <Stat label="AVG LATENCY" value={`${totals.avg_latency_ms}ms`} sub="p50-ish (mean)" />
                    <Stat label="TOKENS" value={totals.total_tokens.toLocaleString()} sub="across window" />
                </section>

                <section className="px-8 py-4 flex items-center gap-3 border-b" style={{ borderColor: 'var(--line-1)' }}>
                    <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>STATUS</span>
                    <Segmented
                        value={(filters.status ?? 'all') as 'all' | 'ok' | 'refused' | 'error'}
                        onChange={(v) => setStatus(v as 'all' | 'ok' | 'refused' | 'error')}
                        options={[
                            { value: 'all', label: 'All' },
                            { value: 'ok', label: 'OK' },
                            { value: 'refused', label: 'Refused' },
                            { value: 'error', label: 'Error' },
                        ]}
                    />
                </section>

                {empty ? (
                    <div className="px-8 py-12">
                        <EmptyState
                            title="No audit rows in this window."
                            detail="Run a few queries from Chat to populate the ledger. Each LLM call writes to audit.query_audit_log with full hash-chain anchoring."
                            action={
                                <Link
                                    href={`/projects/${project.slug}/chat`}
                                    className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                    style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                                >
                                    Open Chat →
                                </Link>
                            }
                        />
                    </div>
                ) : (
                    <section className="px-8 py-6">
                        <Card eyebrow={`ROWS · ${rows.length}`} padded={false}>
                            <div className="grid grid-cols-[80px_1fr_120px_70px_80px_90px_60px] text-[10px] font-mono uppercase tracking-wider px-4 py-2 border-b" style={{ color: 'var(--fg-3)', borderColor: 'var(--line-1)' }}>
                                <div>Status</div>
                                <div>Question</div>
                                <div>Class</div>
                                <div>Conf</div>
                                <div>Tokens</div>
                                <div>Latency</div>
                                <div>Cites</div>
                            </div>
                            {rows.map((r) => (
                                <div key={r.id} className="grid grid-cols-[80px_1fr_120px_70px_80px_90px_60px] items-center text-xs px-4 py-2 border-b" style={{ borderColor: 'var(--line-1)' }}>
                                    <div><Pill tone={r.status === 'ok' ? 'accent' : r.status === 'refused' ? 'warn' : 'danger'} dot>{r.status}</Pill></div>
                                    <div className="truncate" style={{ color: 'var(--fg-0)' }} title={r.query_text}>{r.query_text || <em style={{ color: 'var(--fg-3)' }}>(empty)</em>}</div>
                                    <div className="font-mono" style={{ color: 'var(--fg-2)' }}>{r.query_class ?? '—'}</div>
                                    <div className="font-mono" style={{ color: r.confidence !== null && r.confidence >= 0.7 ? 'var(--accent)' : 'var(--fg-2)' }}>
                                        {r.confidence !== null ? r.confidence.toFixed(2) : '—'}
                                    </div>
                                    <div className="font-mono" style={{ color: 'var(--fg-2)' }}>{r.tokens ?? '—'}</div>
                                    <div className="font-mono" style={{ color: 'var(--fg-2)' }}>{r.latency_ms !== null ? `${r.latency_ms}ms` : '—'}</div>
                                    <div className="font-mono" style={{ color: 'var(--fg-2)' }}>{r.citation_count}</div>
                                </div>
                            ))}
                        </Card>
                    </section>
                )}
            </div>
        </AppLayout>
    );
}
