import type { JSX } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';
import { StatCard, SectionCard, DataTable, EmptyState } from './_shared';

interface PageProps {
    reports: {
        report_id: string; title: string; company: string | null;
        filing_date: string | null; commodity: string | null;
        project_name: string | null; region: string | null;
        created_at: string | null;
    }[];
    by_commodity: { commodity: string; n: number }[];
    totals: { total: number; with_filing_date: number };
}

function fmtDate(iso: string | null): string {
    if (!iso) return '—';
    return new Date(iso).toISOString().slice(0, 10);
}

export default function Reporting({ reports, by_commodity, totals }: PageProps): JSX.Element {
    // Phase 6 — reuses the Phase 2 admin.reports channel. generate_report
    // broadcasts on every successful build; same dispatch already fires
    // for the Admin/ReportBuilder sibling page.
    useAdminSurfaceUpdated('reports', null, () => {
        router.reload({ only: ['reports', 'by_commodity', 'totals'] });
    });

    return (
        <AppLayout>
            <Head title="Reporting" />
            <div className="mx-auto max-w-7xl space-y-6 px-4 py-6 sm:px-6 lg:px-8">
                <div>
                    <h1 className="text-2xl font-semibold text-zinc-900">Reporting</h1>
                    <p className="mt-1 text-sm text-zinc-500">§16.1 · NI 43-101 + workspace report inventory</p>
                </div>

                <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                    <StatCard label="Total reports" value={totals.total.toLocaleString()} />
                    <StatCard label="With filing date" value={totals.with_filing_date.toLocaleString()} sub={totals.total > 0 ? `${Math.round(totals.with_filing_date / totals.total * 100)}% coverage` : ''} />
                    <StatCard label="Commodities tracked" value={by_commodity.length} />
                </div>

                <SectionCard title="By commodity">
                    <DataTable
                        rows={by_commodity}
                        columns={[
                            { key: 'commodity', label: 'Commodity' },
                            { key: 'n', label: 'Reports' },
                        ]}
                    />
                </SectionCard>

                <SectionCard title="Recent reports" sub="up to 50 most recent">
                    {reports.length === 0 ? (
                        <EmptyState message="No reports indexed yet — ingest some NI 43-101 PDFs to populate this." />
                    ) : (
                        <DataTable
                            rows={reports}
                            columns={[
                                { key: 'title', label: 'Title' },
                                { key: 'company', label: 'Company' },
                                { key: 'commodity', label: 'Commodity' },
                                { key: 'region', label: 'Region' },
                                { key: 'filing_date', label: 'Filed', render: (v) => fmtDate(v as string | null) },
                            ]}
                        />
                    )}
                </SectionCard>
            </div>
        </AppLayout>
    );
}
