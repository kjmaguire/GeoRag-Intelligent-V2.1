/**
 * DrillTrace3D.test.tsx
 *
 * Coverage (ADR-0007 PR-4):
 *   - Renders with collars only (no regression from pre-PR-4 behavior).
 *   - Renders curve-tube polylines when collars carry `trace_points`.
 *   - Color-codes intervals — one scatter3d trace per unique color_hint.
 *   - Renders structural pole markers with type-keyed colors + tooltips.
 *   - Tooltip text includes the right fields per overlay type.
 *   - Empty intervals/structures arrays add no extra traces (clean degrade).
 *
 * Test rewrite 2026-05-26: DrillTrace3D no longer uses
 * `react-plotly.js/factory` (rolldown CJS-to-ESM interop crash — see the
 * docblock + workaround at the top of DrillTrace3D.tsx and the original
 * diagnosis in GeoPlot.tsx). The component now drives Plotly imperatively
 * via `PlotlyAPI.react(div, traces, layout, config)`. We mock the default
 * export of `plotly.js-dist-min` with a spy that captures every `.react`
 * call so assertions can introspect the traces array DrillTrace3D built.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, cleanup } from '@testing-library/react';

// jsdom doesn't ship ResizeObserver; DrillTrace3D wires one on mount.
// Provide a no-op so the effect doesn't throw during render.
if (typeof globalThis.ResizeObserver === 'undefined') {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (globalThis as any).ResizeObserver = class {
        observe() { /* noop */ }
        unobserve() { /* noop */ }
        disconnect() { /* noop */ }
    };
}

// Capture every PlotlyAPI.react(el, traces, layout, config) call so the
// tests can read back what DrillTrace3D handed to Plotly. Cleared in
// beforeEach so each test starts with an empty log.
type ReactCall = { el: HTMLElement; traces: unknown[]; layout: unknown; config: unknown };
const reactCalls: ReactCall[] = [];

vi.mock('plotly.js-dist-min', () => {
    const api = {
        react: vi.fn((el: HTMLElement, traces: unknown[], layout: unknown, config: unknown) => {
            reactCalls.push({ el, traces, layout, config });
        }),
        purge: vi.fn(),
        Plots: { resize: vi.fn() },
    };
    // DrillTrace3D reads `(Plotly as any).default ?? Plotly` to cope with
    // the CJS interop variance — give it both shapes for safety.
    return { default: api, ...api };
});

import DrillTrace3D, {
    type CollarPoint,
    type IntervalPoint,
    type StructurePoint,
} from '../DrillTrace3D';

// Trace shape extractor — mirrors what the legacy factory mock used to
// surface so the per-test assertions read naturally.
interface FlatTrace {
    type: string;
    mode: string;
    name: string;
    color: string | undefined;
    width: number | undefined;
    symbol: string | undefined;
    pointCount: number;
    text: unknown;
    hovertemplate: string | null;
    showlegend: boolean;
    xs: unknown;
    ys: unknown;
    zs: unknown;
}

function flatten(traces: unknown[]): FlatTrace[] {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (traces as any[]).map((t) => ({
        type: t.type,
        mode: t.mode,
        name: t.name,
        color: t.line?.color ?? t.marker?.color,
        width: t.line?.width,
        symbol: t.marker?.symbol,
        pointCount: Array.isArray(t.x) ? t.x.length : 0,
        text: t.text ?? null,
        hovertemplate: t.hovertemplate ?? null,
        showlegend: t.showlegend !== false,
        xs: t.x ?? null,
        ys: t.y ?? null,
        zs: t.z ?? null,
    }));
}

function getTraces(): FlatTrace[] {
    if (reactCalls.length === 0) {
        throw new Error('PlotlyAPI.react was never called — effect did not fire');
    }
    return flatten(reactCalls[reactCalls.length - 1].traces);
}

function getTraceCount(): number {
    return getTraces().length;
}

beforeEach(() => {
    reactCalls.length = 0;
});

// ── Fixtures ───────────────────────────────────────────────────────────────

const COLLAR_BASIC: CollarPoint = {
    hole_id: 'HOLE-001',
    collar_id: 'c-001',
    longitude: -105.0,
    latitude: 50.0,
    elevation: 1000,
    total_depth: 200,
    hole_type: 'DDH',
    status: 'Completed',
    azimuth: 0,
    dip: -90,
};

const COLLAR_WITH_TRACE: CollarPoint = {
    ...COLLAR_BASIC,
    hole_id: 'HOLE-002',
    collar_id: 'c-002',
    trace_points: [
        { x: -105.0, y: 50.0, z: 1000, depth_m: 0 },
        { x: -105.001, y: 50.001, z: 900, depth_m: 100 },
        { x: -105.002, y: 50.002, z: 800, depth_m: 200 },
    ],
};

// ── Baseline behavior ──────────────────────────────────────────────────────

describe('DrillTrace3D — baseline (collars only)', () => {
    it('renders nothing in the data array when collars is empty', () => {
        render(<DrillTrace3D collars={[]} />);
        expect(getTraceCount()).toBe(0);
    });

    it('renders one collar-scatter trace + one tube per hole when no trace_points are present', () => {
        render(<DrillTrace3D collars={[COLLAR_BASIC]} />);
        const traces = getTraces();
        expect(traces).toHaveLength(2);

        const markers = traces.find((t) => t.mode === 'markers+text');
        expect(markers).toBeDefined();
        expect(markers!.name).toBe('Completed');
        expect(markers!.color).toBe('#22c55e');

        const tube = traces.find((t) => t.mode === 'lines' && t.showlegend === false);
        expect(tube).toBeDefined();
        // Synthesized 2-point straight trace: top → bottom
        expect(tube!.pointCount).toBe(2);
        expect(tube!.zs).toEqual([1000, 800]); // elevation → elevation - total_depth
    });

    it('emits no extra traces when intervals & structures are empty arrays', () => {
        render(<DrillTrace3D collars={[COLLAR_BASIC]} intervals={[]} structures={[]} />);
        expect(getTraceCount()).toBe(2);
    });
});

// ── trace_points support ───────────────────────────────────────────────────

describe('DrillTrace3D — trace_points', () => {
    it('uses payload-supplied trace_points when length >= 2', () => {
        render(<DrillTrace3D collars={[COLLAR_WITH_TRACE]} />);
        const traces = getTraces();
        const tube = traces.find((t) => t.mode === 'lines' && t.showlegend === false);
        expect(tube!.pointCount).toBe(3);
        expect(tube!.zs).toEqual([1000, 900, 800]);
        expect(tube!.xs).toEqual([-105.0, -105.001, -105.002]);
    });
});

// ── Interval overlays ──────────────────────────────────────────────────────

describe('DrillTrace3D — intervals', () => {
    const INTERVALS: IntervalPoint[] = [
        // depth 0–50 — color A
        { collar_id: 'c-001', depth_from: 0, depth_to: 50, interval_kind: 'lithology', color_hint: '#a83232', label: 'Granite' },
        // depth 50–100 — color B
        { collar_id: 'c-001', depth_from: 50, depth_to: 100, interval_kind: 'alteration', color_hint: '#3232a8', label: 'Sericite' },
        // depth 100–150 — color A again (must batch with the first)
        { collar_id: 'c-001', depth_from: 100, depth_to: 150, interval_kind: 'lithology', color_hint: '#a83232', label: 'Granite' },
    ];

    beforeEach(() => {
        render(<DrillTrace3D collars={[COLLAR_BASIC]} intervals={INTERVALS} />);
    });

    it('produces one interval-trace per unique color_hint (not per interval)', () => {
        const traces = getTraces();
        // 1 collar markers + 1 tube + 2 interval color groups (#a83232, #3232a8)
        expect(traces.filter((t) => t.name?.startsWith('Interval ')).length).toBe(2);
        expect(traces).toHaveLength(4);
    });

    it('batches multi-segments per color via null separators in xs/ys/zs', () => {
        const traces = getTraces();
        const aTrace = traces.find((t) => t.name === 'Interval #a83232');
        // 2 segments × 3 entries (from, to, null) = 6
        expect(aTrace!.xs).toHaveLength(6);
        expect((aTrace!.xs as unknown[])[2]).toBeNull();
        expect((aTrace!.xs as unknown[])[5]).toBeNull();
        expect(aTrace!.color).toBe('#a83232');
    });

    it('builds interval tooltip text "label · from-to m"', () => {
        const traces = getTraces();
        const aTrace = traces.find((t) => t.name === 'Interval #a83232');
        expect(aTrace!.text).toContain('Granite · 0-50m');
        expect(aTrace!.text).toContain('Granite · 100-150m');
    });

    it('drops intervals whose collar_id has no matching collar (graceful degrade)', () => {
        cleanup();
        reactCalls.length = 0;
        render(
            <DrillTrace3D
                collars={[COLLAR_BASIC]}
                intervals={[
                    { collar_id: 'c-MISSING', depth_from: 10, depth_to: 20, interval_kind: 'assay', color_hint: '#ffffff', label: 'Au' },
                ]}
            />,
        );
        // Just collar markers + tube — no interval trace
        expect(getTraceCount()).toBe(2);
    });
});

// ── Structural pole markers ────────────────────────────────────────────────

describe('DrillTrace3D — structures', () => {
    const STRUCTURES: StructurePoint[] = [
        { collar_id: 'c-001', depth: 25, structure_type: 'foliation', strike_deg: 45, dip_deg: 60, source_row_id: 'r1' },
        { collar_id: 'c-001', depth: 80, structure_type: 'foliation', strike_deg: 50, dip_deg: 65, source_row_id: 'r2' },
        { collar_id: 'c-001', depth: 120, structure_type: 'fault', strike_deg: null, dip_deg: null, source_row_id: 'r3' },
    ];

    beforeEach(() => {
        render(<DrillTrace3D collars={[COLLAR_BASIC]} structures={STRUCTURES} />);
    });

    it('produces one trace per unique structure_type', () => {
        const traces = getTraces();
        // 1 collar markers + 1 tube + 2 structure groups (foliation, fault)
        expect(traces).toHaveLength(4);
        const kinds = traces
            .filter((t) => ['foliation', 'fault'].includes(t.name))
            .map((t) => t.name)
            .sort();
        expect(kinds).toEqual(['fault', 'foliation']);
    });

    it('colors foliation blue and fault orange (type→color mapping)', () => {
        const traces = getTraces();
        const fol = traces.find((t) => t.name === 'foliation');
        const fau = traces.find((t) => t.name === 'fault');
        expect(fol!.color).toBe('#3b82f6');
        expect(fau!.color).toBe('#f97316');
        expect(fol!.symbol).toBe('diamond');
    });

    it('builds tooltip "type · depth m · strike X/dip Y" with em-dash on null strike/dip', () => {
        const traces = getTraces();
        const fol = traces.find((t) => t.name === 'foliation');
        const fau = traces.find((t) => t.name === 'fault');
        expect(fol!.text).toEqual([
            'foliation · 25m · strike 45/dip 60',
            'foliation · 80m · strike 50/dip 65',
        ]);
        expect(fau!.text).toEqual(['fault · 120m · strike —/dip —']);
    });

    it('drops structures whose collar_id has no matching collar', () => {
        cleanup();
        reactCalls.length = 0;
        render(
            <DrillTrace3D
                collars={[COLLAR_BASIC]}
                structures={[
                    { collar_id: 'c-MISSING', depth: 30, structure_type: 'joint', strike_deg: 10, dip_deg: 20, source_row_id: 'rx' },
                ]}
            />,
        );
        expect(getTraceCount()).toBe(2);
    });
});

// ── Combined payload smoke ─────────────────────────────────────────────────

describe('DrillTrace3D — all overlays together', () => {
    it('renders collars + curve tube + intervals + structures cleanly', () => {
        render(
            <DrillTrace3D
                collars={[COLLAR_WITH_TRACE]}
                intervals={[
                    { collar_id: 'c-002', depth_from: 0, depth_to: 100, interval_kind: 'lithology', color_hint: '#a83232', label: 'Granite' },
                ]}
                structures={[
                    { collar_id: 'c-002', depth: 150, structure_type: 'vein', strike_deg: 0, dip_deg: 45, source_row_id: 'r1' },
                ]}
            />,
        );
        // 1 collar markers + 1 curve tube + 1 interval color + 1 structure type
        expect(getTraceCount()).toBe(4);
        const traces = getTraces();
        const vein = traces.find((t) => t.name === 'vein');
        expect(vein!.color).toBe('#22c55e'); // green
    });
});

// ── Imperative-API contract ────────────────────────────────────────────────

describe('DrillTrace3D — Plotly imperative API contract', () => {
    it('calls PlotlyAPI.react with (div, traces, layout, config) on mount', () => {
        render(<DrillTrace3D collars={[COLLAR_BASIC]} />);
        expect(reactCalls.length).toBeGreaterThan(0);
        const last = reactCalls[reactCalls.length - 1];
        expect(last.el).toBeInstanceOf(HTMLElement);
        expect(Array.isArray(last.traces)).toBe(true);
        expect(typeof last.layout).toBe('object');
        expect(typeof last.config).toBe('object');
    });
});
