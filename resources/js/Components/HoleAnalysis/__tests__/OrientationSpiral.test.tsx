/**
 * OrientationSpiral's 2-D Plan and Section views (GIS audit 2026-10, finding 4).
 *
 * The Plan view anchored its y axis to x (1 m east = 1 m north on screen); the
 * Section view did not, so it stretched to fill its box and every dip read
 * steeper or flatter than it is.
 */
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../GeoPlot', () => ({ default: () => null }));

import { buildPlan, buildSection, buildTrajectory } from '../OrientationSpiral';

// A 45-degree hole: 100 m along, 100 m down in the section.
const traj = buildTrajectory([], 90, -45, 500, 141.42135623730951);

describe('OrientationSpiral section view', () => {
    it('keeps equal scale on both axes, so a 45 degree hole is drawn at 45 degrees', () => {
        const { layout } = buildSection(traj, 141.42, 500);
        expect(layout.yaxis.scaleanchor).toBe('x');
        expect(layout.yaxis.scaleratio).toBe(1);
    });

    it('does so whether the y axis is elevation or depth below the collar', () => {
        expect(buildSection(traj, 141.42, 500).layout.yaxis.scaleanchor).toBe('x');
        expect(buildSection(traj, 141.42, null).layout.yaxis.scaleanchor).toBe('x');
    });

    it('draws the 45 degree hole with equal horizontal and vertical extent', () => {
        const { traces } = buildSection(traj, 141.42, 500);
        const t = traces[0] as { x: number[]; y: number[] };
        const dx = t.x[t.x.length - 1] - t.x[0];
        const dy = Math.abs(t.y[t.y.length - 1] - t.y[0]);
        expect(dx).toBeCloseTo(100, 0);
        expect(dy).toBeCloseTo(100, 0);
    });
});

describe('OrientationSpiral plan view', () => {
    it('is still equal-aspect', () => {
        expect(buildPlan(traj, 141.42).layout.yaxis.scaleanchor).toBe('x');
    });
});
