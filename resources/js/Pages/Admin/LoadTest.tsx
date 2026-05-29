import type { JSX } from 'react';
import { Head } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';

/**
 * /admin/load-test — §11.9 k6 launcher catalogue.
 *
 * Lists the 3 k6 scripts shipped in tests/load_k6/. Triggering is
 * operator-side (docker run grafana/k6 ...) — the UI surfaces the
 * script catalogue + the canonical commands. Results land in the
 * §16 load-test Grafana dashboard.
 */

type Script = {
    slug: string;
    title: string;
    path: string;
    description: string;
};

type PageProps = { scripts: Script[]; fastapi_error: string | null };

function exampleCommand(s: Script): string {
    return [
        `docker run --rm -i --network host \\`,
        `  -e GEORAG_BASE_URL -e GEORAG_BEARER_TOKEN -e GEORAG_WORKSPACE_ID \\`,
        `  -v "$PWD/tests/load_k6:/scripts" \\`,
        `  grafana/k6 run /scripts/${s.path.split('/').pop()}`,
    ].join('\n');
}

export default function LoadTest({ scripts, fastapi_error }: PageProps): JSX.Element {
    return (
        <AppLayout>
            <Head title="k6 Load Test" />
            <div className="px-6 py-4">
                <h1 className="text-2xl font-semibold mb-2">k6 Load Test Harness</h1>
                <p className="text-sm text-gray-600 mb-4">
                    §11.9 load-test scripts. Each script defines a 4-stage
                    profile (warmup → steady → peak → cool-down) with
                    SLO thresholds. Trigger from an operator shell;
                    results land in the load-test Grafana dashboard.
                </p>

                {fastapi_error && (
                    <div className="mb-3 p-3 bg-red-50 text-red-800 text-sm rounded">
                        Could not reach FastAPI: {fastapi_error}
                    </div>
                )}

                <div className="space-y-4">
                    {scripts.map(s => (
                        <div key={s.slug} className="p-3 border rounded bg-white">
                            <div className="flex justify-between items-baseline">
                                <h2 className="text-lg font-semibold">{s.title}</h2>
                                <code className="text-xs text-gray-500 font-mono">{s.path}</code>
                            </div>
                            <p className="text-sm text-gray-600 mt-1 mb-2">{s.description}</p>
                            <pre className="bg-gray-50 p-2 rounded text-xs font-mono whitespace-pre-wrap">
                                {exampleCommand(s)}
                            </pre>
                        </div>
                    ))}
                </div>

                <div className="mt-6 p-3 bg-blue-50 border border-blue-200 rounded text-sm">
                    <strong>Results:</strong> Each run writes a JSON
                    summary to <code>tests/load_k6/results/&lt;script&gt;-&lt;ts&gt;.json</code>
                    when invoked with <code>--summary-export=</code>. CI's{' '}
                    <code>load-smoke</code> job runs all scripts at
                    <code> --vus 5 --duration 30s</code> on every PR; full load
                    runs happen nightly via <code>nightly-load-test</code>.
                </div>
            </div>
        </AppLayout>
    );
}
