import { useEffect, useMemo, useRef } from 'react';
import Plotly from 'plotly.js-dist-min';

/**
 * Generic Plotly wrapper — bypasses `react-plotly.js/factory` entirely.
 *
 * Why: rolldown's CJS-to-ESM interop wraps the factory module such that
 * `(wrappedNamespace).default` points at the wrapper itself rather than
 * the original function export, crashing with
 * "(0, o.default) is not a function" on first call. Tried multiple
 * workarounds (static import, lazy chunk, namespace import, dynamic
 * import) — all hit the same rolldown transformation.
 *
 * The fix: use Plotly's native imperative API directly. `Plotly.newPlot`
 * and `Plotly.react` are stable, documented, and don't require any
 * React wrapper. We host them inside a ref'd div and call them from
 * useEffect — same lifecycle semantics as a React component wrapper,
 * zero factory involvement.
 */

interface GeoPlotProps {
    data: Record<string, unknown>[];
    layout: Record<string, unknown>;
}

// Default-importing a CJS module through rolldown can yield either the
// wrapper namespace or the real module depending on __esModule flags.
// plotly.js-dist-min surfaces its API as the default export AND as the
// module namespace members — this coalesces both shapes.
const PlotlyAPI: any = (Plotly as any).default ?? Plotly;

const DEFAULT_CONFIG = {
    responsive: true,
    displaylogo: false,
    modeBarButtonsToRemove: [
        'toImage', 'sendDataToCloud', 'editInChartStudio',
        'lasso2d', 'select2d',
    ],
};

export default function GeoPlot({ data, layout }: GeoPlotProps) {
    const divRef = useRef<HTMLDivElement | null>(null);

    const mergedLayout = useMemo(
        () => ({ ...layout, autosize: true }),
        [layout],
    );

    // Mount + update: Plotly.react is the idempotent update call —
    // equivalent to setState for the plot. On unmount, purge to free
    // the WebGL context.
    useEffect(() => {
        const el = divRef.current;
        if (!el) return;
        if (typeof PlotlyAPI?.react !== 'function') {
            // Should be unreachable — surfaced as visible error if the
            // module import ever breaks so we don't fail silently.
            // eslint-disable-next-line no-console
            console.error('[GeoPlot] Plotly API missing .react method:', PlotlyAPI);
            return;
        }
        PlotlyAPI.react(el, data, mergedLayout, DEFAULT_CONFIG);
    }, [data, mergedLayout]);

    useEffect(() => {
        const el = divRef.current;
        return () => {
            if (el && typeof PlotlyAPI?.purge === 'function') {
                PlotlyAPI.purge(el);
            }
        };
    }, []);

    // Resize handler — Plotly needs an explicit call when its container
    // changes size (e.g. tab becomes visible, sidebar resizes).
    useEffect(() => {
        const el = divRef.current;
        if (!el || typeof ResizeObserver === 'undefined') return;
        const ro = new ResizeObserver(() => {
            if (typeof PlotlyAPI?.Plots?.resize === 'function') {
                try { PlotlyAPI.Plots.resize(el); } catch { /* ignore */ }
            }
        });
        ro.observe(el);
        return () => ro.disconnect();
    }, []);

    return (
        <div
            ref={divRef}
            className="w-full h-full"
            style={{ width: '100%', height: '100%' }}
        />
    );
}
