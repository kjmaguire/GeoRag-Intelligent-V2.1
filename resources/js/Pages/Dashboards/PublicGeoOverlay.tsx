import type { JSX } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { usePublicGeoscienceTileInvalidation } from '@/Hooks/useTileInvalidation';
import { StatCard, SectionCard, DataTable } from './_shared';

interface PageProps {
    counts: {
        occurrences: number; drillholes: number; mines: number;
        bedrock_polygons: number; assessment_surveys: number;
    };
    sources: {
        source_id: string; jurisdiction_code: string; name: string;
        canonical_type: string; license_summary: string | null;
        last_refreshed_at: string | null;
    }[];
    by_jurisdiction: {
        jurisdiction_code: string;
        occurrences: number; drillholes: number; mines: number;
    }[];
}

function fmtTime(iso: string | null): string {
    if (!iso) return '(never)';
    const d = new Date(iso);
    const days = Math.floor((Date.now() - d.getTime()) / 86400000);
    if (days === 0) return 'today';
    if (days === 1) return 'yesterday';
    if (days < 30) return `${days} d ago`;
    return d.toISOString().slice(0, 10);
}

export default function PublicGeoOverlay({ counts, sources, by_jurisdiction }: PageProps): JSX.Element {
    // Phase 6 — reuses the Phase 4 public-geoscience.tiles channel.
    // public_geoscience_pull (Kestra→Hatchet, every 6 h) broadcasts on
    // successful pulls; this dashboard's counts/sources/jurisdictions
    // all change with the same writer.
    usePublicGeoscienceTileInvalidation(() => {
        router.reload({ only: ['counts', 'sources', 'by_jurisdiction'] });
    });

    return (
        <AppLayout>
            <Head title="PublicGeo Overlay" />
            <div className="mx-auto max-w-7xl space-y-6 px-4 py-6 sm:px-6 lg:px-8">
                <div>
                    <h1 className="text-2xl font-semibold text-zinc-900">PublicGeo Overlay</h1>
                    <p className="mt-1 text-sm text-zinc-500">§16.1 · public-geoscience inventory + connector freshness</p>
                </div>

                <div className="grid grid-cols-2 gap-4 sm:grid-cols-5">
                    <StatCard label="Mineral occurrences" value={counts.occurrences.toLocaleString()} />
                    <StatCard label="Drillhole collars" value={counts.drillholes.toLocaleString()} />
                    <StatCard label="Mines" value={counts.mines.toLocaleString()} />
                    <StatCard label="Bedrock polygons" value={counts.bedrock_polygons.toLocaleString()} />
                    <StatCard label="Assessment surveys" value={counts.assessment_surveys.toLocaleString()} />
                </div>

                <SectionCard title="By jurisdiction" sub="row counts per province / state">
                    <DataTable
                        rows={by_jurisdiction}
                        columns={[
                            { key: 'jurisdiction_code', label: 'Jurisdiction' },
                            { key: 'occurrences',  label: 'Occurrences' },
                            { key: 'drillholes',   label: 'Drillholes' },
                            { key: 'mines',        label: 'Mines' },
                        ]}
                    />
                </SectionCard>

                <SectionCard title="Registered sources" sub="last refresh from each upstream feed">
                    <DataTable
                        rows={sources}
                        columns={[
                            { key: 'source_id', label: 'Source' },
                            { key: 'jurisdiction_code', label: 'Jurisdiction' },
                            { key: 'canonical_type', label: 'Type' },
                            { key: 'license_summary', label: 'License' },
                            {
                                key: 'last_refreshed_at', label: 'Last refresh',
                                render: (v) => fmtTime(v as string | null),
                            },
                        ]}
                    />
                </SectionCard>
            </div>
        </AppLayout>
    );
}
