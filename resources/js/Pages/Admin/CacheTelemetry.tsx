import type { JSX } from 'react';
import { useEffect, useState } from 'react';
import { Head } from '@inertiajs/react';
import AppLayout from '../../Layouts/AppLayout';

/**
 * /admin/cache-telemetry — Phase 38 R-P21-CACHE-TELEMETRY-DASHBOARD
 * frontend slice. Consumes the Phase 37 JSON endpoint
 * (/admin/cache-telemetry/skip-reasons.json) and renders the cache
 * health view: 24h + 1h hit/miss totals plus per-skip-reason
 * breakdown.
 *
 * Self-fetching (no server-side Inertia props) so the page can
 * refresh on a button click without a full Inertia round-trip.
 * The endpoint is admin-gated; this page mounts under AppLayout
 * which is itself behind auth:sanctum.
 */

interface CacheTotals {
    hits: number;
    misses: number;
    total: number;
    hit_rate: number;
}

interface SkippedReasons {
    zero_candidates: number;
    partial_failures: number;
    schema_validation_failed: number;
    downhole_bypass_legacy: number;
    '(none)': number;
}

interface CacheTelemetryPayload {
    window_hours: number;
    totals: CacheTotals;
    skipped_reasons: SkippedReasons;
    last_hour: CacheTotals;
}

function HitRatePill({ rate }: { rate: number }): JSX.Element {
    // Green for >25%, amber for 5-25%, red for <5%.
    const pct = (rate * 100).toFixed(1);
    let cls = 'text-red-700 bg-red-100';
    if (rate >= 0.25) cls = 'text-green-700 bg-green-100';
    else if (rate >= 0.05) cls = 'text-amber-700 bg-amber-100';
    return (
        <span className={`inline-block px-2 py-0.5 rounded text-sm font-mono ${cls}`}>
            {pct}%
        </span>
    );
}

function TotalsTable({ label, totals }: { label: string; totals: CacheTotals }): JSX.Element {
    return (
        <div className="border rounded p-4 bg-white shadow-sm">
            <h3 className="text-sm font-semibold text-gray-700 mb-2">{label}</h3>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
                <dt className="text-gray-500">Hits</dt>
                <dd className="font-mono">{totals.hits.toLocaleString()}</dd>
                <dt className="text-gray-500">Misses</dt>
                <dd className="font-mono">{totals.misses.toLocaleString()}</dd>
                <dt className="text-gray-500">Total runs</dt>
                <dd className="font-mono">{totals.total.toLocaleString()}</dd>
                <dt className="text-gray-500">Hit rate</dt>
                <dd>
                    <HitRatePill rate={totals.hit_rate} />
                </dd>
            </dl>
        </div>
    );
}

function SkippedReasonsTable({ reasons }: { reasons: SkippedReasons }): JSX.Element {
    const rows: Array<[string, number]> = [
        ['zero_candidates', reasons.zero_candidates],
        ['partial_failures', reasons.partial_failures],
        ['schema_validation_failed', reasons.schema_validation_failed],
        ['downhole_bypass_legacy', reasons.downhole_bypass_legacy],
        ['(none) — wrote successfully', reasons['(none)']],
    ];
    return (
        <div className="border rounded p-4 bg-white shadow-sm">
            <h3 className="text-sm font-semibold text-gray-700 mb-2">Cache-write skip reasons</h3>
            <table className="w-full text-sm">
                <thead>
                    <tr className="text-left text-gray-500 border-b">
                        <th className="pb-2">Reason</th>
                        <th className="pb-2 text-right">Count</th>
                    </tr>
                </thead>
                <tbody>
                    {rows.map(([reason, count]) => (
                        <tr key={reason} className="border-b last:border-0">
                            <td className="py-1 font-mono text-xs">{reason}</td>
                            <td className="py-1 text-right font-mono">{count.toLocaleString()}</td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}

export default function CacheTelemetry(): JSX.Element {
    const [data, setData] = useState<CacheTelemetryPayload | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [loading, setLoading] = useState(true);
    const [windowHours, setWindowHours] = useState<number>(24);

    const fetchData = async (hours: number): Promise<void> => {
        setLoading(true);
        setError(null);
        try {
            const res = await fetch(`/admin/cache-telemetry/skip-reasons.json?window_hours=${hours}`, {
                credentials: 'same-origin',
                headers: { Accept: 'application/json' },
            });
            if (!res.ok) {
                throw new Error(`HTTP ${res.status}`);
            }
            const body = (await res.json()) as CacheTelemetryPayload;
            setData(body);
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        void fetchData(windowHours);
    }, [windowHours]);

    return (
        <AppLayout>
            <Head title="Cache telemetry" />
            <div className="max-w-5xl mx-auto p-6 space-y-6">
                <header>
                    <h1 className="text-2xl font-semibold text-gray-900">Cache telemetry</h1>
                    <p className="text-sm text-gray-600 mt-1">
                        Retrieval-cache health for the deterministic RAG path. Reads
                        from <code className="font-mono text-xs">silver.answer_runs</code>; window
                        rolls forward continuously.
                    </p>
                </header>

                <div className="flex items-center gap-3">
                    <label className="text-sm text-gray-700">
                        Window:
                        <select
                            className="ml-2 border rounded px-2 py-1 text-sm"
                            value={windowHours}
                            onChange={(e) => setWindowHours(Number(e.target.value))}
                        >
                            <option value={1}>1 hour</option>
                            <option value={6}>6 hours</option>
                            <option value={24}>24 hours</option>
                            <option value={168}>7 days</option>
                        </select>
                    </label>
                    <button
                        type="button"
                        className="ml-auto text-sm border rounded px-3 py-1 bg-gray-50 hover:bg-gray-100"
                        onClick={() => void fetchData(windowHours)}
                    >
                        Refresh
                    </button>
                </div>

                {loading && (
                    <div className="text-sm text-gray-500">Loading…</div>
                )}

                {error && (
                    <div className="border border-red-200 bg-red-50 text-red-800 rounded p-3 text-sm">
                        Failed to load cache telemetry: <code className="font-mono">{error}</code>
                    </div>
                )}

                {data && !error && (
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <TotalsTable label={`Last ${data.window_hours} h`} totals={data.totals} />
                        <TotalsTable label="Last 1 h" totals={data.last_hour} />
                        <div className="md:col-span-2">
                            <SkippedReasonsTable reasons={data.skipped_reasons} />
                        </div>
                    </div>
                )}

                <footer className="text-xs text-gray-400 pt-4 border-t">
                    Phase 21 + Phase 30 + Phase 37 telemetry surfaces. The five
                    skip-reason buckets correspond to the
                    <code className="font-mono"> cache_skipped_reason </code>
                    CHECK constraint enum in
                    <code className="font-mono"> silver.answer_runs</code>.
                </footer>
            </div>
        </AppLayout>
    );
}
