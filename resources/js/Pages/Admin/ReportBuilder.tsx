import type { JSX } from 'react';
import { useState } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';
import { useAdminSurfaceUpdated } from '@/Hooks/useAdminSurfaceUpdated';

/**
 * /admin/reports — Phase H4 §7 UI.
 *
 * Picker for one of the 11 §15.2 report types + recent-builds list.
 * Clicking "Build" plans a new report (synchronous report_planner
 * call); the build_id flows to /admin/reports/{build_id}.
 */

type SectionPlan = {
    section_id: string;
    title: string;
    template_slug: string;
    required_evidence_kinds: string[];
    map_kinds: string[];
    chart_kinds: string[];
};

type ReportTypePlan = {
    report_type: string;
    sections: SectionPlan[];
    summary: string;
};

type Manifest = {
    report_types: string[];
    plans: Record<string, ReportTypePlan>;
};

type BuildSummary = {
    build_id: string;
    workspace_id: string;
    project_id: string;
    report_type: string;
    requested_by_user_id: number | null;
    requested_at: string;
    status: string;
    sections_planned: number;
};

type PageProps = {
    manifest: Manifest | null;
    builds: BuildSummary[];
    fastapi_error: string | null;
};

const REPORT_TYPE_LABELS: Record<string, string> = {
    weekly_project_digest: 'Weekly Project Digest',
    ingestion_quality: 'Ingestion Quality',
    technical_due_diligence: 'Technical Due Diligence',
    executive_project_intelligence: 'Executive Project Intelligence',
    gis_arcgis_sync: 'GIS / ArcGIS Sync',
    target_recommendation: 'Target Recommendation',
    public_geo_overlay: 'Public Geoscience Overlay',
    data_room_package: 'Data Room Package',
    what_changed: 'What Changed',
    ni43101_section_pack: 'NI 43-101 Section Pack',
    csa11348_disclosure_pack: 'CSA 11-348 Disclosure Pack',
};

export default function ReportBuilder({ manifest, builds, fastapi_error }: PageProps): JSX.Element {
    // Phase 2 real-time push — generate_report broadcasts to admin.reports
    // on completion (the per-build cockpit channel admin.reports.{build_id}
    // remains for in-progress section progress, untouched).
    useAdminSurfaceUpdated('reports', null, () => {
        router.reload({ only: ['builds'] });
    });

    const [reportType, setReportType] = useState<string>(manifest?.report_types[0] ?? '');
    const [workspaceId, setWorkspaceId] = useState<string>('');
    const [projectId, setProjectId] = useState<string>('');
    const [submitting, setSubmitting] = useState<boolean>(false);
    const [result, setResult] = useState<{ ok: boolean; message: string; build_id?: string } | null>(null);

    async function buildReport(): Promise<void> {
        if (!reportType || !workspaceId || !projectId) {
            setResult({ ok: false, message: 'All fields required.' });
            return;
        }
        setSubmitting(true);
        setResult(null);
        try {
            const csrf = (document.querySelector('meta[name="csrf-token"]') as HTMLMetaElement | null)?.content ?? '';
            const resp = await fetch('/admin/reports/build', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'X-CSRF-TOKEN': csrf,
                },
                body: JSON.stringify({
                    report_type: reportType,
                    workspace_id: workspaceId,
                    project_id: projectId,
                    requested_by_user_id: 1,  // TODO: surface from auth context
                }),
            });
            const body = await resp.json();
            if (resp.ok) {
                setResult({
                    ok: true,
                    message: `Build planned — ${body.sections_planned} section(s).`,
                    build_id: body.build_id,
                });
                router.reload({ only: ['builds'] });
            } else {
                setResult({ ok: false, message: body.error ?? 'Build failed.' });
            }
        } catch (err) {
            setResult({ ok: false, message: `Network error: ${(err as Error).message}` });
        } finally {
            setSubmitting(false);
        }
    }

    const selectedPlan = reportType && manifest?.plans[reportType];

    return (
        <AppLayout>
            <Head title="Report Builder" />
            <div className="px-6 py-4">
                <h1 className="text-2xl font-semibold mb-2">Report Builder</h1>
                <p className="text-sm text-gray-600 mb-4">
                    Plan one of the 11 §15.2 report types. The §7 graph
                    drafts the sections downstream; the §29 export
                    compliance gate validates before any artifact ships.
                </p>

                {fastapi_error && (
                    <div className="mb-3 p-3 bg-red-50 text-red-800 text-sm rounded">
                        Could not reach FastAPI: {fastapi_error}
                    </div>
                )}

                <div className="grid grid-cols-12 gap-4">
                    <div className="col-span-5 p-3 border rounded bg-white">
                        <h2 className="text-lg font-semibold mb-2">New build</h2>
                        <label className="text-sm block">
                            Report type
                            <select
                                className="block w-full mt-1 p-1.5 border rounded"
                                value={reportType}
                                onChange={e => setReportType(e.target.value)}
                            >
                                {manifest?.report_types.map(rt => (
                                    <option key={rt} value={rt}>
                                        {REPORT_TYPE_LABELS[rt] ?? rt}
                                    </option>
                                ))}
                            </select>
                        </label>
                        <label className="text-sm block mt-3">
                            Workspace id (UUID)
                            <input
                                type="text"
                                className="block w-full mt-1 p-1.5 border rounded font-mono text-xs"
                                value={workspaceId}
                                onChange={e => setWorkspaceId(e.target.value)}
                                placeholder="a0000000-…"
                            />
                        </label>
                        <label className="text-sm block mt-3">
                            Project id (UUID)
                            <input
                                type="text"
                                className="block w-full mt-1 p-1.5 border rounded font-mono text-xs"
                                value={projectId}
                                onChange={e => setProjectId(e.target.value)}
                            />
                        </label>
                        <button
                            type="button"
                            onClick={buildReport}
                            disabled={submitting}
                            className="mt-3 px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:bg-gray-300"
                        >
                            {submitting ? 'Planning…' : 'Plan build'}
                        </button>
                        {result && (
                            <div className={`mt-3 p-2 rounded text-sm ${
                                result.ok ? 'bg-green-50 text-green-800' : 'bg-red-50 text-red-800'
                            }`}>
                                {result.message}
                                {result.build_id && (
                                    <>
                                        {' '}
                                        <Link
                                            href={`/admin/reports/${result.build_id}`}
                                            className="underline"
                                        >
                                            Open build
                                        </Link>
                                    </>
                                )}
                            </div>
                        )}
                    </div>

                    <div className="col-span-7">
                        <h2 className="text-lg font-semibold mb-2">
                            Selected template — {REPORT_TYPE_LABELS[reportType] ?? reportType}
                        </h2>
                        {selectedPlan ? (
                            <table className="w-full text-sm border-collapse">
                                <thead>
                                    <tr className="bg-gray-50 text-left">
                                        <th className="py-2 px-2">Section</th>
                                        <th className="py-2 px-2">Template</th>
                                        <th className="py-2 px-2">Evidence</th>
                                        <th className="py-2 px-2">Visuals</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {selectedPlan.sections.map(s => (
                                        <tr key={s.section_id} className="border-b">
                                            <td className="py-2 px-2 font-medium">{s.title}</td>
                                            <td className="py-2 px-2 text-xs font-mono text-gray-600">
                                                {s.template_slug}
                                            </td>
                                            <td className="py-2 px-2 text-xs text-gray-600">
                                                {s.required_evidence_kinds.join(', ') || '—'}
                                            </td>
                                            <td className="py-2 px-2 text-xs text-gray-600">
                                                {[...s.map_kinds, ...s.chart_kinds].join(', ') || '—'}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        ) : (
                            <p className="text-gray-500">No template selected.</p>
                        )}
                    </div>
                </div>

                <h2 className="text-lg font-semibold mt-8 mb-2">Recent builds</h2>
                <table className="w-full text-sm border-collapse">
                    <thead>
                        <tr className="bg-gray-50 text-left">
                            <th className="py-2 px-2">Build</th>
                            <th className="py-2 px-2">Type</th>
                            <th className="py-2 px-2">Workspace</th>
                            <th className="py-2 px-2 text-right">Sections</th>
                            <th className="py-2 px-2">Status</th>
                            <th className="py-2 px-2">Requested</th>
                        </tr>
                    </thead>
                    <tbody>
                        {builds.length === 0 && (
                            <tr>
                                <td colSpan={6} className="py-4 text-center text-gray-500">
                                    No builds yet.
                                </td>
                            </tr>
                        )}
                        {builds.map(b => (
                            <tr key={b.build_id} className="border-b hover:bg-gray-50">
                                <td className="py-2 px-2 font-mono text-xs">
                                    <Link href={`/admin/reports/${b.build_id}`} className="text-blue-600 hover:underline">
                                        {b.build_id.slice(0, 8)}…
                                    </Link>
                                </td>
                                <td className="py-2 px-2">{REPORT_TYPE_LABELS[b.report_type] ?? b.report_type}</td>
                                <td className="py-2 px-2 font-mono text-xs">{b.workspace_id.slice(0, 8)}…</td>
                                <td className="py-2 px-2 text-right">{b.sections_planned}</td>
                                <td className="py-2 px-2">{b.status}</td>
                                <td className="py-2 px-2 text-xs text-gray-600">
                                    {new Date(b.requested_at).toLocaleString()}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </AppLayout>
    );
}
