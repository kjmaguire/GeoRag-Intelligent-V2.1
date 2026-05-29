import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, Stat, ProgressBar, EmptyState } from '@/Components/Foundry/primitives';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';
import type { IngestQualityProps } from '@/Types/Foundry';

/**
 * Foundry IngestQuality — post-import trust-moment surface.
 *
 * Reads silver.document_ingestion_quality + silver.low_confidence_page_reviews
 * + silver.bronze_provenance via FoundryIngestQualityController.
 */
export default function FoundryIngestQuality({ import_id, project, files, anomalies, totals, pass_gate, empty }: IngestQualityProps) {
    const total = totals.accepted + totals.flagged + totals.rejected;
    const acceptPct = total === 0 ? 0 : Math.round((totals.accepted / total) * 100);

    // Reliability spec Phase 2b — refetch when an ingest run completes
    // and the MV refresh confirms. ingest_pdf + drill-upload both write
    // silver.document_ingestion_quality, so the 'quality' and 'reports'
    // affected_types both warrant a reload of the trust-report props.
    useWorkspaceDataUpdated(project.project_id, (event) => {
        if (
            event.affected_types.includes('quality') ||
            event.affected_types.includes('reports')
        ) {
            router.reload({
                only: ['files', 'anomalies', 'totals', 'pass_gate', 'empty'],
            });
        }
    });

    return (
        <AppLayout>
            <Head title={`Ingest quality · ${project.project_name}`} />

            <div className="flex-1 overflow-y-auto" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · INGEST QUALITY`}
                    title="Trust moment"
                    sub={`Import ${import_id} · ${files.length} files inspected`}
                    actions={
                        <Link
                            href={`/projects/${project.slug}/data`}
                            className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                            style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                        >
                            ← Data
                        </Link>
                    }
                />

                {empty ? (
                    <div className="px-8 py-12">
                        <EmptyState
                            title="No ingest validation records for this project yet."
                            detail="Run an import (Data → Connect Source) to populate the trust report. Existing imports will appear here once Dagster writes silver.document_ingestion_quality rows."
                        />
                    </div>
                ) : (
                    <>
                        <section className="grid grid-cols-2 sm:grid-cols-4 gap-px px-8 py-5" style={{ background: 'var(--line-1)' }}>
                            <Stat label="ACCEPTED" value={String(totals.accepted)} tone="accent" />
                            <Stat label="FLAGGED" value={String(totals.flagged)} sub={totals.flagged > 0 ? 'needs review' : 'clean'} />
                            <Stat label="REJECTED" value={String(totals.rejected)} />
                            <Stat label="AWAITING OCR" value={String(totals.awaiting_ocr)} sub="Tier-2 pipeline" />
                        </section>

                        <section className="px-8 py-5 flex items-center gap-4">
                            <div className="flex-1">
                                <div className="flex justify-between text-xs mb-1">
                                    <span style={{ color: 'var(--fg-2)' }}>Acceptance rate</span>
                                    <span className="font-mono" style={{ color: 'var(--fg-0)' }}>{acceptPct}%</span>
                                </div>
                                <ProgressBar value={acceptPct} tone={pass_gate ? 'accent' : 'warn'} height={8} />
                            </div>
                            <div>
                                {pass_gate ? (
                                    <Pill tone="accent" dot>Bronze → Silver gate: PASS</Pill>
                                ) : (
                                    <Pill tone="warn" dot>Gate blocked</Pill>
                                )}
                            </div>
                        </section>

                        <section className="px-8 pb-8">
                            <Card eyebrow="FILES" title={`${files.length} ingested`} padded={false}>
                                <div className="grid grid-cols-[1fr_80px_80px_80px_100px_100px] text-[10px] font-mono uppercase tracking-wider px-4 py-2 border-b" style={{ color: 'var(--fg-3)', borderColor: 'var(--line-1)' }}>
                                    <div>Name</div>
                                    <div>Format</div>
                                    <div>Rows</div>
                                    <div>Accepted</div>
                                    <div>CRS</div>
                                    <div>Status</div>
                                </div>
                                {files.map((f) => (
                                    <div
                                        key={f.file_id}
                                        className="grid grid-cols-[1fr_80px_80px_80px_100px_100px] text-xs px-4 py-2 border-b items-center"
                                        style={{ borderColor: 'var(--line-1)' }}
                                    >
                                        <div className="truncate" style={{ color: 'var(--fg-0)' }}>{f.name}</div>
                                        <div className="font-mono" style={{ color: 'var(--fg-2)' }}>{f.format}</div>
                                        <div className="font-mono" style={{ color: 'var(--fg-2)' }}>{f.rows ?? '—'}</div>
                                        <div className="font-mono" style={{ color: f.accepted !== null && f.rows && f.accepted < f.rows ? 'var(--warn)' : 'var(--fg-1)' }}>
                                            {f.accepted ?? '—'}
                                        </div>
                                        <div className="font-mono" style={{ color: 'var(--fg-2)' }}>{f.crs_detected ?? '—'}</div>
                                        <div>
                                            <Pill tone={statusToneFor(f.status)} dot>
                                                {f.status.replace('_', ' ')}
                                            </Pill>
                                        </div>
                                    </div>
                                ))}
                            </Card>

                            {anomalies.length > 0 && (
                                <Card eyebrow={`ANOMALIES · ${anomalies.length}`} className="mt-4">
                                    <table className="w-full text-xs">
                                        <thead>
                                            <tr style={{ color: 'var(--fg-3)' }}>
                                                <th className="text-left font-mono uppercase tracking-wider py-1">Row</th>
                                                <th className="text-left font-mono uppercase tracking-wider py-1">Column</th>
                                                <th className="text-left font-mono uppercase tracking-wider py-1">Value</th>
                                                <th className="text-left font-mono uppercase tracking-wider py-1">Rule</th>
                                                <th className="text-left font-mono uppercase tracking-wider py-1">Action</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {anomalies.map((a, i) => (
                                                <tr key={i} className="border-t" style={{ borderColor: 'var(--line-1)' }}>
                                                    <td className="py-1.5 font-mono" style={{ color: 'var(--fg-2)' }}>{a.row ?? '—'}</td>
                                                    <td className="py-1.5 font-mono" style={{ color: 'var(--fg-1)' }}>{a.column ?? '—'}</td>
                                                    <td className="py-1.5 font-mono" style={{ color: 'var(--fg-1)' }}>{a.value ?? '—'}</td>
                                                    <td className="py-1.5" style={{ color: 'var(--fg-1)' }}>{a.rule}</td>
                                                    <td className="py-1.5"><Pill tone={a.action === 'reject' ? 'danger' : 'warn'}>{a.action}</Pill></td>
                                                </tr>
                                            ))}
                                        </tbody>
                                    </table>
                                </Card>
                            )}
                        </section>
                    </>
                )}
            </div>
        </AppLayout>
    );
}

function statusToneFor(s: string): 'accent' | 'warn' | 'danger' | 'info' | 'neutral' {
    if (s === 'ok') return 'accent';
    if (s === 'awaiting_ocr') return 'info';
    if (s === 'regex_incomplete' || s === 'warn') return 'warn';
    if (s === 'error') return 'danger';
    return 'neutral';
}
