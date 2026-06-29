import { Head, Link, router } from '@inertiajs/react';
import { useMemo, useState } from 'react';
import type { JSX } from 'react';
import AppLayout from '@/Layouts/AppLayout';
import { Card, EmptyState, PageHeader, Pill, Stat } from '@/Components/Foundry/primitives';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';

/**
 * CC-01 Item 1 Slice 4 — Foundry DrillReview.
 *
 * Lists silver.review_queue rows grouped by ingest batch (bronze_uri),
 * surfaces per-row confidence + outlier flags, and routes the reviewer's
 * decision back to DrillReviewController@decide.
 */

type Decision = 'approve_as_parsed' | 'approve_with_corrections' | 'reject' | 'defer';

interface QueueRow {
    queue_id: string;
    target_table: string;
    target_record_kind: string;
    bronze_uri: string;
    bronze_row_offset: number | null;
    payload: Record<string, unknown> | null;
    confidence_per_field: Record<string, number> | null;
    confidence_record: number | null;
    outlier_flags: Array<Record<string, string[]>> | null;
    routing_decision: string | null;
    routing_reason: string | null;
    lifecycle: string;
    decision_kind: string | null;
    parser_version: string;
    created_at: string;
}

interface Batch {
    bronze_uri: string;
    target_tables: string[];
    rows: QueueRow[];
    row_count: number;
    oldest: string;
}

interface Props {
    project: { project_id: string; project_name: string; slug: string };
    batches: Batch[];
    counters: { pending: number; in_review: number; decided: number };
    decisions: Decision[];
}

function confidenceTone(score: number | null): string {
    if (score === null) return 'var(--fg-3)';
    if (score >= 0.8) return 'var(--accent)';
    if (score >= 0.5) return 'var(--warn)';
    return 'var(--danger)';
}

function flagsList(flags: Array<Record<string, string[]>> | null): Array<{ category: string; items: string[] }> {
    if (!flags || flags.length === 0) return [];
    const out: Array<{ category: string; items: string[] }> = [];
    for (const entry of flags) {
        for (const [category, items] of Object.entries(entry)) {
            if (Array.isArray(items) && items.length > 0) {
                out.push({ category, items });
            }
        }
    }
    return out;
}

export default function FoundryDrillReview({ project, batches, counters, decisions }: Props) {
    // Reliability spec Phase 2b — drill-upload Dagster runs commit_ingestion_run
    // which broadcasts ingestion.progress 'completed' → DebounceWorkspaceMvRefresh
    // emits WorkspaceDataUpdated with affected_types including 'review_queue'.
    // The review-queue rows for the upload land in silver.review_queue before
    // the broadcast fires, so a partial reload here surfaces them immediately.
    useWorkspaceDataUpdated(project.project_id, (event) => {
        if (event.affected_types.includes('review_queue')) {
            router.reload({ only: ['batches', 'counters'] });
        }
    });

    return (
        <AppLayout>
            <Head title={`Drill review · ${project.project_name}`} />

            <div className="flex-1 overflow-y-auto" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · DRILL REVIEW`}
                    title="Review queue"
                    sub={`Pending decisions on parsed drill data before it commits to silver tables.`}
                    actions={
                        <Link
                            href={`/projects/${project.slug}/lakehouse`}
                            className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                            style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                        >
                            ← Lakehouse
                        </Link>
                    }
                />

                <section
                    className="grid grid-cols-3 gap-px px-8 py-5"
                    style={{ background: 'var(--line-1)' }}
                >
                    <Stat label="PENDING" value={String(counters.pending)} />
                    <Stat label="IN REVIEW" value={String(counters.in_review)} />
                    <Stat label="DECIDED" value={String(counters.decided)} sub="awaiting commit" />
                </section>

                {batches.length === 0 ? (
                    <div className="px-8 py-12">
                        <EmptyState
                            title="No review queue rows for this project."
                            detail="Upload drill data via the import flow — rows that need a reviewer's eye will land here. Clean rows commit directly to silver.* without surfacing."
                        />
                    </div>
                ) : (
                    <div className="px-8 pb-12 space-y-6">
                        {batches.map((b) => (
                            <BatchCard
                                key={b.bronze_uri}
                                batch={b}
                                projectSlug={project.slug}
                                decisions={decisions}
                            />
                        ))}
                    </div>
                )}
            </div>
        </AppLayout>
    );
}

function BatchCard({
    batch,
    projectSlug,
    decisions,
}: {
    batch: Batch;
    projectSlug: string;
    decisions: Decision[];
}): JSX.Element {
    return (
        <Card
            eyebrow={`BATCH · ${batch.target_tables.join(' · ')}`}
            title={`${batch.row_count} row${batch.row_count === 1 ? '' : 's'} · ${batch.bronze_uri}`}
            padded={false}
        >
            <div className="divide-y" style={{ borderColor: 'var(--line-1)' }}>
                {batch.rows.map((row) => (
                    <QueueRowItem
                        key={row.queue_id}
                        row={row}
                        projectSlug={projectSlug}
                        decisions={decisions}
                    />
                ))}
            </div>
        </Card>
    );
}

function QueueRowItem({
    row,
    projectSlug,
    decisions,
}: {
    row: QueueRow;
    projectSlug: string;
    decisions: Decision[];
}): JSX.Element {
    const flags = useMemo(() => flagsList(row.outlier_flags), [row.outlier_flags]);
    const payloadEntries = useMemo(
        () => (row.payload ? Object.entries(row.payload) : []),
        [row.payload],
    );

    const [processing, setProcessing] = useState(false);

    const submit = (kind: Decision) => {
        setProcessing(true);
        router.post(
            `/projects/${projectSlug}/drill-review/${row.queue_id}/decide`,
            { decision_kind: kind },
            {
                preserveScroll: true,
                onFinish: () => setProcessing(false),
            },
        );
    };

    return (
        <div className="px-4 py-3 grid grid-cols-[1fr_220px] gap-4 items-start">
            <div className="space-y-2 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                    <Pill tone={row.lifecycle === 'decided' ? 'accent' : 'neutral'}>{row.lifecycle}</Pill>
                    <Pill tone="neutral">{row.target_table}</Pill>
                    <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                        {row.parser_version}
                    </span>
                    <span
                        className="ml-auto text-[10px] font-mono"
                        style={{ color: confidenceTone(row.confidence_record) }}
                    >
                        confidence · {row.confidence_record === null ? '—' : row.confidence_record.toFixed(2)}
                    </span>
                </div>

                {flags.length > 0 && (
                    <div className="rounded p-2" style={{ background: 'var(--bg-1)', border: '1px solid var(--line-2)' }}>
                        {flags.map((f) => (
                            <div key={f.category} className="space-y-1">
                                <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--warn)' }}>
                                    {f.category}
                                </div>
                                <ul className="text-xs space-y-0.5" style={{ color: 'var(--fg-1)' }}>
                                    {f.items.map((s, i) => (
                                        <li key={i}>· {s}</li>
                                    ))}
                                </ul>
                            </div>
                        ))}
                    </div>
                )}

                {payloadEntries.length > 0 && (
                    <PayloadHeatmap
                        entries={payloadEntries}
                        confidencePerField={row.confidence_per_field}
                    />
                )}

                {row.routing_reason && (
                    <div className="text-[11px]" style={{ color: 'var(--fg-3)' }}>
                        routing: {row.routing_reason}
                    </div>
                )}
            </div>

            <div className="space-y-2">
                {row.lifecycle === 'decided' ? (
                    <Pill tone="accent" dot>
                        {row.decision_kind ?? 'decided'}
                    </Pill>
                ) : (
                    <>
                        {decisions.map((d) => (
                            <button
                                key={d}
                                disabled={processing}
                                onClick={() => submit(d)}
                                className="block w-full text-left text-[11px] font-mono uppercase tracking-wider px-2 py-1 rounded border disabled:opacity-50"
                                style={{
                                    color: d === 'reject' ? 'var(--danger)' : d === 'defer' ? 'var(--fg-2)' : 'var(--fg-0)',
                                    borderColor: 'var(--line-2)',
                                    background: 'var(--bg-1)',
                                }}
                            >
                                {d.replace(/_/g, ' ')}
                            </button>
                        ))}
                    </>
                )}
            </div>
        </div>
    );
}

function PayloadHeatmap({
    entries,
    confidencePerField,
}: {
    entries: Array<[string, unknown]>;
    confidencePerField: Record<string, number> | null;
}): JSX.Element {
    return (
        <div className="grid grid-cols-2 gap-px" style={{ background: 'var(--line-1)' }}>
            {entries.map(([key, value]) => {
                const conf = confidencePerField?.[key] ?? null;
                return (
                    <div
                        key={key}
                        className="px-2 py-1"
                        style={{
                            background: 'var(--bg-0)',
                            borderLeft: `3px solid ${confidenceTone(conf)}`,
                        }}
                    >
                        <div
                            className="text-[10px] font-mono uppercase tracking-wider"
                            style={{ color: 'var(--fg-3)' }}
                        >
                            {key}
                        </div>
                        <div
                            className="text-xs font-mono truncate"
                            style={{ color: 'var(--fg-1)' }}
                            title={String(value ?? '')}
                        >
                            {value === null || value === undefined ? '—' : String(value)}
                        </div>
                    </div>
                );
            })}
        </div>
    );
}
