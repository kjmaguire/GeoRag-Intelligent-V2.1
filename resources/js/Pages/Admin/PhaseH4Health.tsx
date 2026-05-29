import type { JSX } from 'react';
import { useState } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';

/**
 * /admin/phase-h4-health — operator surface for the Phase H4 composite
 * health check. Lights up every dependency the Phase H4 admin pages need:
 * tables, indexes, RLS state, service-key env var, pg pool.
 *
 * Useful as a first stop after restoring a stack or before promoting a
 * deploy.
 */

type Check = {
    name: string;
    ok: boolean;
    detail: string | null;
};

type Health = {
    ok: boolean;
    checks: Check[];
    timestamp: string;
};

type PageProps = {
    health: Health | null;
    fastapi_error: string | null;
};

export default function PhaseH4Health({ health, fastapi_error }: PageProps): JSX.Element {
    const [refreshing, setRefreshing] = useState<boolean>(false);

    const reload = (): void => {
        setRefreshing(true);
        router.reload({
            only: ['health', 'fastapi_error'],
            onFinish: () => setRefreshing(false),
        });
    };

    const passCount = health?.checks.filter(c => c.ok).length ?? 0;
    const failCount = health?.checks.filter(c => !c.ok).length ?? 0;

    return (
        <AppLayout>
            <Head title="Phase H4 Health" />
            <div className="px-6 py-4">
                <div className="flex items-baseline justify-between mb-2">
                    <h1 className="text-2xl font-semibold">Phase H4 Health</h1>
                    <button
                        type="button"
                        onClick={reload}
                        disabled={refreshing}
                        className="px-3 py-1 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:bg-gray-300"
                    >
                        {refreshing ? 'Refreshing…' : 'Refresh'}
                    </button>
                </div>

                <p className="text-sm text-gray-600 mb-4">
                    Composite check across every Phase H4 dependency. Polls
                    <code className="ml-1 mr-1">/api/v1/admin/phase-h4-health</code>
                    on each refresh. A green result means every Phase H4 admin
                    page can serve its UI; investigate individual rows first
                    before opening downstream cockpits.
                </p>

                {fastapi_error && (
                    <div className="mb-3 p-3 bg-red-50 border border-red-200 text-red-800 text-sm rounded">
                        FastAPI unreachable: {fastapi_error}
                    </div>
                )}

                {health && (
                    <>
                        <div className={`mb-4 p-3 rounded border ${
                            health.ok
                                ? 'bg-green-50 border-green-200 text-green-900'
                                : 'bg-red-50 border-red-200 text-red-900'
                        }`}>
                            <div className="flex justify-between">
                                <div>
                                    <span className="text-2xl">{health.ok ? '✓' : '✗'}</span>{' '}
                                    <span className="font-semibold">
                                        {health.ok ? 'All checks pass' : 'At least one check failed'}
                                    </span>
                                </div>
                                <span className="text-xs font-mono">
                                    {passCount} pass · {failCount} fail · sampled {new Date(health.timestamp).toLocaleString()}
                                </span>
                            </div>
                        </div>

                        <table className="w-full text-sm border-collapse">
                            <thead>
                                <tr className="bg-gray-50 text-left">
                                    <th className="py-2 px-2 w-12"> </th>
                                    <th className="py-2 px-2">Check</th>
                                    <th className="py-2 px-2">Detail</th>
                                </tr>
                            </thead>
                            <tbody>
                                {health.checks.map(c => (
                                    <tr key={c.name} className="border-b hover:bg-gray-50">
                                        <td className="py-2 px-2 text-center">
                                            {c.ok ? (
                                                <span className="text-green-600 text-lg">✓</span>
                                            ) : (
                                                <span className="text-red-600 text-lg">✗</span>
                                            )}
                                        </td>
                                        <td className="py-2 px-2 font-mono text-xs">{c.name}</td>
                                        <td className="py-2 px-2 text-xs text-gray-600">
                                            {c.detail ?? (c.ok ? <span className="text-gray-400">—</span> : '')}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </>
                )}
            </div>
        </AppLayout>
    );
}
