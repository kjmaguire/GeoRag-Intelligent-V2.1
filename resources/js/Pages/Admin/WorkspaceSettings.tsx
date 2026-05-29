import type { JSX } from 'react';
import { useState } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';

type Settings = {
    workspace_id: string;
    default_tone: 'technical' | 'executive' | 'regulator';
    default_report_type: string | null;
    sla_max_response_ms: number | null;
    extra_payload: Record<string, unknown>;
};

type PageProps = { workspace_id: string; settings: Settings | null; fastapi_error: string | null };

export default function WorkspaceSettings({ workspace_id, settings, fastapi_error }: PageProps): JSX.Element {
    const [tone, setTone] = useState<Settings['default_tone']>(settings?.default_tone ?? 'technical');
    const [reportType, setReportType] = useState<string>(settings?.default_report_type ?? '');
    const [sla, setSla] = useState<string>(settings?.sla_max_response_ms?.toString() ?? '');
    const [busy, setBusy] = useState<boolean>(false);
    const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);

    async function save(): Promise<void> {
        setBusy(true); setResult(null);
        const csrf = (document.querySelector('meta[name="csrf-token"]') as HTMLMetaElement | null)?.content ?? '';
        try {
            const r = await fetch(`/admin/workspace-settings/${workspace_id}`, {
                method: 'POST', credentials: 'include',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-TOKEN': csrf },
                body: JSON.stringify({
                    default_tone: tone,
                    default_report_type: reportType || null,
                    sla_max_response_ms: sla ? parseInt(sla, 10) : null,
                }),
            });
            const j = await r.json();
            if (r.ok) setResult({ ok: true, message: 'Saved.' });
            else setResult({ ok: false, message: j.error ?? 'Save failed.' });
        } catch (e) {
            setResult({ ok: false, message: `Network: ${(e as Error).message}` });
        } finally {
            setBusy(false);
        }
    }

    return (
        <AppLayout>
            <Head title="Workspace Settings" />
            <div className="px-6 py-4">
                <div className="mb-2">
                    <Link href="/admin/workspace-members" className="text-blue-600 text-sm hover:underline">← Members</Link>
                </div>
                <h1 className="text-2xl font-semibold mb-2">Workspace Settings</h1>
                <p className="text-sm text-gray-600 mb-4">
                    Workspace <code className="font-mono text-xs">{workspace_id}</code>. Per-workspace preferences
                    that drive defaults across the §7 Report Builder, §11 chat, and §10 support cockpit.
                </p>

                {fastapi_error && <div className="mb-3 p-3 bg-red-50 text-red-800 text-sm rounded">{fastapi_error}</div>}

                <div className="p-3 border rounded bg-white max-w-2xl">
                    <label className="text-sm block">
                        Default tone (Presentation Coach §7.6)
                        <select className="block w-full mt-1 p-1.5 border rounded"
                                value={tone} onChange={e => setTone(e.target.value as Settings['default_tone'])}>
                            <option value="technical">Technical</option>
                            <option value="executive">Executive</option>
                            <option value="regulator">Regulator (NI 43-101 / CSA 11-348)</option>
                        </select>
                    </label>
                    <label className="text-sm block mt-3">
                        Default report type (optional)
                        <input className="block w-full mt-1 p-1.5 border rounded font-mono text-xs"
                               value={reportType} onChange={e => setReportType(e.target.value)} placeholder="weekly_project_digest" />
                    </label>
                    <label className="text-sm block mt-3">
                        SLA max response (ms, optional)
                        <input type="number" className="block w-full mt-1 p-1.5 border rounded"
                               value={sla} onChange={e => setSla(e.target.value)} placeholder="5000" />
                    </label>
                    <button type="button" onClick={save} disabled={busy}
                            className="mt-3 px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:bg-gray-300">
                        {busy ? 'Saving…' : 'Save settings'}
                    </button>
                    {result && (
                        <div className={`mt-3 p-2 rounded text-sm ${result.ok ? 'bg-green-50 text-green-800' : 'bg-red-50 text-red-800'}`}>
                            {result.message}
                        </div>
                    )}
                </div>
            </div>
        </AppLayout>
    );
}
