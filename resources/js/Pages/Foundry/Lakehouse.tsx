import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, Stat, EmptyState } from '@/Components/Foundry/primitives';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';

/**
 * Foundry Lakehouse — single-page Bronze + Silver + Gold inventory.
 *
 * Phase-22 §B/S/G build-out. Anchored-scroll layout with three sections
 * (Bronze / Silver / Gold). Each section shows per-table row counts plus
 * the most-recently-created rows; drill-in links go to existing detail
 * surfaces (IngestQuality, Sources, Hole Compare, etc.).
 *
 * Schema-not-provisioned states render the table card with a muted
 * "missing" pill rather than the row. This keeps the page functional on
 * environments mid-migration without 500s.
 */

type Scope = 'project' | 'workspace' | 'global';

interface TableSummary {
    exists: boolean;
    count: number;
    recent: Array<Record<string, unknown>>;
    error?: string;
    /**
     * Blast radius of the row count:
     *   - 'project'   — filtered by the current project_id
     *   - 'workspace' — filtered by the current workspace_id (cross-project)
     *   - 'global'    — no scoping column on the table; count is across
     *                   every tenant in the cluster
     * The controller sets this per table so the UI can show the right pill.
     */
    scope?: Scope;
}

interface LakehouseProps {
    project: {
        project_id: string;
        project_name: string;
        slug: string;
    };
    bronze: Record<string, TableSummary>;
    silver: Record<string, TableSummary>;
    gold: Record<string, TableSummary>;
}

const BRONZE_TABLES: Array<{ key: string; label: string }> = [
    { key: 'source_files',    label: 'source_files' },
    { key: 'ingest_manifest', label: 'ingest_manifest' },
    { key: 'provenance',      label: 'provenance' },
];

const SILVER_TABLES: Array<{ key: string; label: string }> = [
    { key: 'collars',             label: 'collars' },
    { key: 'lithology_intervals', label: 'lithology_intervals' },
    { key: 'assays_v2',           label: 'assays_v2' },
    { key: 'structures',          label: 'structures' },
    { key: 'geophysics_surveys',  label: 'geophysics_surveys' },
    { key: 'spatial_features',    label: 'spatial_features' },
    { key: 'raster_layers',       label: 'raster_layers' },
    { key: 'reports',             label: 'reports' },
];

const GOLD_TABLES: Array<{ key: string; label: string }> = [
    { key: 'drillhole_intervals_visual',    label: 'drillhole_intervals_visual' },
    { key: 'cross_section_panels',          label: 'cross_section_panels' },
    { key: 'structure_measurements_visual', label: 'structure_measurements_visual' },
    { key: 'h3_density',                    label: 'h3_density' },
];

function totalRows(summary: Record<string, TableSummary>): number {
    return Object.values(summary).reduce((acc, t) => acc + (t?.count ?? 0), 0);
}

function tablesProvisioned(summary: Record<string, TableSummary>): number {
    return Object.values(summary).filter(t => t?.exists).length;
}

export default function Lakehouse({ project, bronze, silver, gold }: LakehouseProps) {
    // Reliability spec Phase 2b — Lakehouse reads counts off silver/gold
    // tables that change every time an ingestion completes. Refetch the
    // relevant props on workspace.data_updated; skip if nothing this page
    // cares about was touched.
    useWorkspaceDataUpdated(project.project_id, (evt) => {
        const t = evt.affected_types;
        const props: string[] = [];
        if (t.includes('reports') || t.includes('collars') || t.includes('assays')) {
            props.push('bronze', 'silver', 'gold');
        }
        if (props.length > 0) {
            router.reload({ only: props });
        }
    });

    return (
        <AppLayout>
            <Head title={`Lakehouse · ${project.project_name}`} />

            <div className="flex-1 overflow-y-auto" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · LAKEHOUSE`}
                    title="Bronze + Silver + Gold inventory"
                    sub="Every layer of ingested data for this project, in one place."
                    actions={
                        <Link
                            href={`/projects/${project.slug}`}
                            className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                            style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                        >
                            ← Project
                        </Link>
                    }
                />

                <section className="grid grid-cols-3 gap-px px-8 py-5" style={{ background: 'var(--line-1)' }}>
                    <Stat
                        label="BRONZE"
                        value={String(totalRows(bronze))}
                        sub={`${tablesProvisioned(bronze)}/${BRONZE_TABLES.length} tables · raw source files`}
                    />
                    <Stat
                        label="SILVER"
                        value={String(totalRows(silver))}
                        sub={`${tablesProvisioned(silver)}/${SILVER_TABLES.length} tables · canonical entities`}
                        tone="accent"
                    />
                    <Stat
                        label="GOLD"
                        value={String(totalRows(gold))}
                        sub={`${tablesProvisioned(gold)}/${GOLD_TABLES.length} tables · pre-computed visuals`}
                    />
                </section>

                <LayerSection
                    title="Bronze"
                    subtitle="Raw files + ingest manifest + per-row provenance. Drill-in: Sources page."
                    tables={BRONZE_TABLES}
                    summary={bronze}
                    drillIn={`/projects/${project.slug}/sources`}
                />

                <LayerSection
                    title="Silver"
                    subtitle="Canonical entities (collars, lithology, assays, surveys, vector / raster features, reports). Drill-in: Hole Compare + IngestQuality."
                    tables={SILVER_TABLES}
                    summary={silver}
                    drillIn={`/projects/${project.slug}/compare`}
                />

                <LayerSection
                    title="Gold"
                    subtitle="Pre-computed strip-logs, cross-section panels, stereonet projections, H3 density grids. Drill-in: Drillhole Detail (per hole)."
                    tables={GOLD_TABLES}
                    summary={gold}
                    drillIn={`/projects/${project.slug}/workspace`}
                />
            </div>
        </AppLayout>
    );
}

function LayerSection({
    title,
    subtitle,
    tables,
    summary,
    drillIn,
}: {
    title: string;
    subtitle: string;
    tables: Array<{ key: string; label: string }>;
    summary: Record<string, TableSummary>;
    drillIn: string;
}) {
    return (
        <section className="px-8 py-6">
            <Card
                eyebrow={title.toUpperCase()}
                title={subtitle}
                padded={false}
                actions={
                    <Link
                        href={drillIn}
                        className="text-xs font-mono uppercase tracking-wider px-2 py-1 rounded border"
                        style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                    >
                        Drill in →
                    </Link>
                }
            >
                <div className="grid grid-cols-[1fr_80px_88px_80px_1fr] text-[10px] font-mono uppercase tracking-wider px-4 py-2 border-b" style={{ color: 'var(--fg-3)', borderColor: 'var(--line-1)' }}>
                    <div>Table</div>
                    <div>Rows</div>
                    <div>Scope</div>
                    <div>Status</div>
                    <div>Latest</div>
                </div>
                {tables.map(({ key, label }) => {
                    const t = summary[key];
                    const recent = t?.recent?.[0];
                    const latestLabel = recent ? formatRecent(recent) : '—';
                    return (
                        <div
                            key={key}
                            className="grid grid-cols-[1fr_80px_88px_80px_1fr] text-xs px-4 py-2 border-b items-center"
                            style={{ borderColor: 'var(--line-1)' }}
                        >
                            <div className="truncate" style={{ color: 'var(--fg-0)' }}>
                                {title.toLowerCase()}.{label}
                            </div>
                            <div className="font-mono" style={{ color: 'var(--fg-2)' }}>
                                {t?.exists ? t.count.toLocaleString() : '—'}
                            </div>
                            <div>
                                <ScopePill scope={t?.scope} />
                            </div>
                            <div>
                                {t?.exists ? (
                                    <Pill tone={t.count > 0 ? 'accent' : 'neutral'} dot>
                                        {t.count > 0 ? 'populated' : 'empty'}
                                    </Pill>
                                ) : (
                                    <Pill tone="warn" dot>missing</Pill>
                                )}
                            </div>
                            <div className="truncate font-mono" style={{ color: 'var(--fg-3)' }}>
                                {latestLabel}
                            </div>
                        </div>
                    );
                })}
                {tables.every(({ key }) => !summary[key]?.exists || summary[key]?.count === 0) && (
                    <div className="px-4 py-6">
                        <EmptyState
                            title={`No ${title.toLowerCase()}-layer rows for this project yet.`}
                            detail={`Run an import or the ${title.toLowerCase()} pipeline to populate these tables.`}
                        />
                    </div>
                )}
            </Card>
        </section>
    );
}

/**
 * Renders a scope badge next to each table's row count.
 *
 *   - project   → 'accent' (the common, scoped case)
 *   - workspace → 'neutral' (cross-project but tenant-isolated)
 *   - global    → 'warn'    (CROSS-TENANT — surface clearly so users
 *                  understand the count isn't filtered)
 *
 * The 'global' badge is the important one: bronze.ingest_manifest and
 * bronze.provenance currently have no tenancy column or RLS, so their
 * counts are across the entire cluster. Calling that out in the UI is
 * the honest interim until the proper RLS migration lands.
 */
function ScopePill({ scope }: { scope?: Scope }) {
    if (!scope) return <span style={{ color: 'var(--fg-3)' }}>—</span>;
    const label = scope.toUpperCase();
    const tone: 'accent' | 'neutral' | 'warn' =
        scope === 'project' ? 'accent' : scope === 'workspace' ? 'neutral' : 'warn';
    return <Pill tone={tone} dot>{label}</Pill>;
}

function formatRecent(row: Record<string, unknown>): string {
    const created = (row.created_at ?? row.computed_at) as string | undefined;
    const name =
        (row.feature_name as string | undefined) ??
        (row.survey_name as string | undefined) ??
        (row.hole_id as string | undefined) ??
        (row.layer_name as string | undefined) ??
        (row.title as string | undefined) ??
        '';
    return name ? `${name} · ${created ?? ''}` : (created ?? '—');
}
