import type { JSX } from 'react';
import { useState } from 'react';
import { Head } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';

/**
 * /admin/dashboards — Phase H4 §16 UI.
 *
 * Catalogue of the 16 Grafana dashboards shipped in
 * docker/grafana/dashboards/. Operators can open one in an embedded
 * iframe (default) or pop into a new tab.
 */

type Dashboard = {
    slug: string;
    uid: string;
    title: string;
    audience: string;     // 'ops' | 'product'
    description: string;
};

type PageProps = {
    grafana_base_url: string;
    dashboards: Dashboard[];
};

export default function Dashboards({ grafana_base_url, dashboards }: PageProps): JSX.Element {
    const [selected, setSelected] = useState<Dashboard | null>(dashboards[0] ?? null);

    const opsDashboards = dashboards.filter(d => d.audience === 'ops');
    const productDashboards = dashboards.filter(d => d.audience === 'product');

    function iframeUrl(d: Dashboard): string {
        return `${grafana_base_url}/d/${d.uid}/${d.slug}?kiosk=tv&theme=light`;
    }

    function externalUrl(d: Dashboard): string {
        return `${grafana_base_url}/d/${d.uid}/${d.slug}`;
    }

    return (
        <AppLayout>
            <Head title="Grafana Dashboards" />
            <div className="px-6 py-4">
                <h1 className="text-2xl font-semibold mb-2">Grafana Dashboards</h1>
                <p className="text-sm text-gray-600 mb-4">
                    §16 dashboards. <code>{grafana_base_url}</code> is the
                    configured Grafana base URL (override via{' '}
                    <code>GRAFANA_BASE_URL</code>).
                </p>

                <div className="grid grid-cols-12 gap-4">
                    <div className="col-span-3 space-y-4">
                        <div>
                            <h2 className="text-sm font-semibold uppercase text-gray-500 mb-2">
                                Product ({productDashboards.length})
                            </h2>
                            <ul className="space-y-1">
                                {productDashboards.map(d => (
                                    <li key={d.slug}>
                                        <button
                                            type="button"
                                            onClick={() => setSelected(d)}
                                            className={`w-full text-left p-2 rounded border text-sm ${
                                                selected?.slug === d.slug
                                                    ? 'border-blue-500 bg-blue-50'
                                                    : 'border-gray-200 hover:bg-gray-50'
                                            }`}
                                        >
                                            {d.title}
                                        </button>
                                    </li>
                                ))}
                            </ul>
                        </div>
                        <div>
                            <h2 className="text-sm font-semibold uppercase text-gray-500 mb-2">
                                Ops ({opsDashboards.length})
                            </h2>
                            <ul className="space-y-1">
                                {opsDashboards.map(d => (
                                    <li key={d.slug}>
                                        <button
                                            type="button"
                                            onClick={() => setSelected(d)}
                                            className={`w-full text-left p-2 rounded border text-sm ${
                                                selected?.slug === d.slug
                                                    ? 'border-blue-500 bg-blue-50'
                                                    : 'border-gray-200 hover:bg-gray-50'
                                            }`}
                                        >
                                            {d.title}
                                        </button>
                                    </li>
                                ))}
                            </ul>
                        </div>
                    </div>

                    <div className="col-span-9">
                        {selected ? (
                            <>
                                <div className="flex justify-between items-baseline mb-2">
                                    <div>
                                        <h2 className="text-lg font-semibold">
                                            {selected.title}
                                        </h2>
                                        <p className="text-sm text-gray-600">
                                            {selected.description}
                                        </p>
                                    </div>
                                    <a
                                        href={externalUrl(selected)}
                                        target="_blank"
                                        rel="noopener noreferrer"
                                        className="text-sm text-blue-600 hover:underline"
                                    >
                                        Open in Grafana ↗
                                    </a>
                                </div>
                                <iframe
                                    src={iframeUrl(selected)}
                                    className="w-full h-[75vh] border rounded bg-white"
                                    title={selected.title}
                                />
                            </>
                        ) : (
                            <p className="text-gray-500">No dashboard selected.</p>
                        )}
                    </div>
                </div>
            </div>
        </AppLayout>
    );
}
