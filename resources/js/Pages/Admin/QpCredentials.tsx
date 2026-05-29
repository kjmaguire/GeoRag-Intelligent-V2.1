import type { JSX } from 'react';
import { useState } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';

type Credential = {
    qp_credential_id: string;
    user_id: number;
    name: string;
    issuing_body: string;
    registration_number: string;
    jurisdiction: string;
    expires_at: string | null;
    verified_at: string | null;
    is_active: boolean;
};

type PageProps = { credentials: Credential[]; fastapi_error: string | null };

export default function QpCredentials({ credentials, fastapi_error }: PageProps): JSX.Element {
    const [name, setName] = useState<string>('');
    const [issuingBody, setIssuingBody] = useState<string>('APGO');
    const [registrationNumber, setRegistrationNumber] = useState<string>('');
    const [jurisdiction, setJurisdiction] = useState<string>('Ontario');
    const [userId, setUserId] = useState<string>('1');
    const [busy, setBusy] = useState<boolean>(false);
    const [error, setError] = useState<string | null>(null);

    async function post(url: string, body: Record<string, unknown>): Promise<Response> {
        const csrf = (document.querySelector('meta[name="csrf-token"]') as HTMLMetaElement | null)?.content ?? '';
        return fetch(url, {
            method: 'POST', credentials: 'include',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRF-TOKEN': csrf },
            body: JSON.stringify(body),
        });
    }

    async function createQp(): Promise<void> {
        setBusy(true); setError(null);
        try {
            const r = await post('/admin/qp-credentials', {
                user_id: parseInt(userId, 10),
                name, issuing_body: issuingBody,
                registration_number: registrationNumber, jurisdiction,
            });
            if (r.ok) {
                router.reload();
            } else {
                const j = await r.json(); setError(j.error ?? 'Create failed');
            }
        } finally {
            setBusy(false);
        }
    }

    async function verifyQp(id: string): Promise<void> {
        const r = await post(`/admin/qp-credentials/${encodeURIComponent(id)}/verify`, {});
        if (r.ok) router.reload();
    }

    return (
        <AppLayout>
            <Head title="QP Credentials" />
            <div className="px-6 py-4">
                <h1 className="text-2xl font-semibold mb-2">QP Credentials</h1>
                <p className="text-sm text-gray-600 mb-4">
                    Manage Qualified Person credentials referenced by the
                    §29.6 R5 sign-off ceremony. Credentials must be marked
                    verified (staffed-ops gate) before sign-off can complete.
                </p>

                {fastapi_error && (
                    <div className="mb-3 p-3 bg-red-50 text-red-800 text-sm rounded">{fastapi_error}</div>
                )}

                <div className="mb-4 p-3 border rounded bg-white">
                    <h2 className="text-md font-semibold mb-2">Register a QP</h2>
                    <div className="grid grid-cols-5 gap-2">
                        <label className="text-sm">User id<input className="block w-full mt-1 p-1.5 border rounded" value={userId} onChange={e => setUserId(e.target.value)} /></label>
                        <label className="text-sm">Name<input className="block w-full mt-1 p-1.5 border rounded" value={name} onChange={e => setName(e.target.value)} placeholder="Jane Smith, P.Geo." /></label>
                        <label className="text-sm">Issuing body<input className="block w-full mt-1 p-1.5 border rounded" value={issuingBody} onChange={e => setIssuingBody(e.target.value)} placeholder="APGO" /></label>
                        <label className="text-sm">Registration #<input className="block w-full mt-1 p-1.5 border rounded" value={registrationNumber} onChange={e => setRegistrationNumber(e.target.value)} /></label>
                        <label className="text-sm">Jurisdiction<input className="block w-full mt-1 p-1.5 border rounded" value={jurisdiction} onChange={e => setJurisdiction(e.target.value)} /></label>
                    </div>
                    <button type="button" onClick={createQp} disabled={busy} className="mt-2 px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:bg-gray-300">
                        {busy ? 'Saving…' : 'Register'}
                    </button>
                    {error && <div className="mt-2 p-2 bg-red-50 text-red-800 text-sm rounded">{error}</div>}
                </div>

                <table className="w-full text-sm border-collapse">
                    <thead>
                        <tr className="bg-gray-50 text-left">
                            <th className="py-2 px-2">QP id</th>
                            <th className="py-2 px-2">Name</th>
                            <th className="py-2 px-2">Body</th>
                            <th className="py-2 px-2">Jurisdiction</th>
                            <th className="py-2 px-2">Verified</th>
                            <th className="py-2 px-2"></th>
                        </tr>
                    </thead>
                    <tbody>
                        {credentials.length === 0 && <tr><td colSpan={6} className="py-6 text-center text-gray-500">No QPs registered.</td></tr>}
                        {credentials.map(c => (
                            <tr key={c.qp_credential_id} className="border-b hover:bg-gray-50">
                                <td className="py-2 px-2 font-mono text-xs">{c.qp_credential_id}</td>
                                <td className="py-2 px-2">{c.name}</td>
                                <td className="py-2 px-2">{c.issuing_body}</td>
                                <td className="py-2 px-2">{c.jurisdiction}</td>
                                <td className="py-2 px-2">
                                    {c.verified_at
                                        ? <span className="px-2 py-0.5 rounded text-xs bg-green-100 text-green-800">verified</span>
                                        : <span className="px-2 py-0.5 rounded text-xs bg-amber-100 text-amber-800">pending</span>}
                                </td>
                                <td className="py-2 px-2">
                                    {!c.verified_at && (
                                        <button type="button" onClick={() => verifyQp(c.qp_credential_id)}
                                                className="text-xs text-blue-600 hover:underline">
                                            Mark verified
                                        </button>
                                    )}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </AppLayout>
    );
}
