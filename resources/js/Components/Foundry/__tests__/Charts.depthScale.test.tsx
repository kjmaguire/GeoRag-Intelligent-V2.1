/**
 * Depth-indexed tracks share one scale.
 *
 * LithologyStripColumn declared `depthMax` and never read it: it fitted its own
 * deepest interval, with 32 px of top room against DownholeMultiLog's 16. So the
 * curve tracks and the geology column of one hole (Workspace LOGS), or the
 * columns of two holes being compared (CompareHolesModal, SectionView), drew a
 * given depth at different heights, and a 130 m hole was stretched to the
 * height of a 400 m one.
 */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import {
    DEPTH_TRACK_BOTTOM,
    DEPTH_TRACK_TOP,
    DownholeMultiLog,
    LithologyStripColumn,
    depthTrackScale,
    geologyDepth,
    roundUpDepth,
    sharedDepthAxis,
    type LithologyInterval,
} from '../Charts';

afterEach(cleanup);

const band = (from: number, to: number, code = 'GRN'): LithologyInterval => ({
    from,
    to,
    code,
    label: code,
    color: '#8899aa',
});

const num = (el: Element, attr: string) => Number(el.getAttribute(attr));

describe('depthTrackScale', () => {
    it('puts depth 0 at the shared top and the axis maximum at the bottom of the plot', () => {
        const s = depthTrackScale(300, 400);
        expect(s.top).toBe(DEPTH_TRACK_TOP);
        expect(s.yOf(0)).toBe(DEPTH_TRACK_TOP);
        expect(s.yOf(400)).toBe(DEPTH_TRACK_TOP + 300);
        expect(s.yOf(100)).toBe(DEPTH_TRACK_TOP + 75);
        expect(s.svgHeight).toBe(DEPTH_TRACK_TOP + 300 + DEPTH_TRACK_BOTTOM);
    });

    it('does not divide by a zero axis', () => {
        expect(Number.isFinite(depthTrackScale(300, 0).yOf(10))).toBe(true);
    });
});

describe('sharedDepthAxis / geologyDepth', () => {
    it('is the deepest thing any track reaches, rounded up to a friendly tick', () => {
        expect(sharedDepthAxis([180, 95])).toBe(roundUpDepth(180));
        expect(sharedDepthAxis([180, 95])).toBe(200);
        expect(sharedDepthAxis([400])).toBe(500);
    });

    it('ignores holes with no recorded depth and curves that are not drawn', () => {
        expect(sharedDepthAxis([null, undefined, Number.NaN, 95])).toBe(100);
        expect(sharedDepthAxis([])).toBe(50);
        expect(sharedDepthAxis([null])).toBe(50);
    });

    it('reads the deepest interval across lithology, alteration and mineralization', () => {
        expect(
            geologyDepth({
                intervals: [{ to: 80 }],
                alteration: [{ to: 140 }],
                mineralization: [{ to: 120 }],
            }),
        ).toBe(140);
        expect(geologyDepth({})).toBe(0);
    });
});

describe('LithologyStripColumn depth scale', () => {
    it('draws a depth at the same y whichever hole it belongs to when both get one depthMax (SectionView, Compare)', () => {
        // A shallow hole and a deep hole on one 400 m axis, 300 px plot.
        const { unmount } = render(
            <LithologyStripColumn intervals={[band(0, 50)]} holeId="SHALLOW" depthMax={400} height={300} />,
        );
        const shallow = screen.getByLabelText('GRN 0-50 m');
        const shallowTop = num(shallow, 'y');
        const shallowHeight = num(shallow, 'height');
        unmount();

        render(
            <LithologyStripColumn
                intervals={[band(0, 50), band(50, 400, 'SST')]}
                holeId="DEEP"
                depthMax={400}
                height={300}
            />,
        );
        const deep = screen.getByLabelText('GRN 0-50 m');
        expect(num(deep, 'y')).toBe(shallowTop);
        expect(num(deep, 'height')).toBe(shallowHeight);
        // 50 m of a 400 m axis is 37.5 px of a 300 px plot - NOT stretched to
        // the hole's own depth, which is what ignoring depthMax did.
        expect(shallowTop).toBe(DEPTH_TRACK_TOP);
        expect(shallowHeight).toBe(37.5);
    });

    it('lines up with curve tracks drawn to the same axis (Workspace LOGS)', () => {
        const { container } = render(
            <>
                <DownholeMultiLog
                    tracks={[
                        {
                            label: 'GAMMA',
                            color: '#fff',
                            min: 0,
                            max: 10,
                            points: [
                                { depth: 0, value: 1 },
                                { depth: 200, value: 5 },
                            ],
                        },
                    ]}
                    depthMax={400}
                    height={300}
                />
                <LithologyStripColumn
                    intervals={[band(0, 100), band(200, 250, 'SST')]}
                    holeId="H1"
                    depthMax={400}
                    height={300}
                />
            </>,
        );
        const [curves, strip] = Array.from(container.querySelectorAll('svg'));

        // Same plot height, same top room: the two SVGs are the same size.
        expect(curves.getAttribute('height')).toBe(strip.getAttribute('height'));
        expect(num(strip, 'height')).toBe(DEPTH_TRACK_TOP + 300 + DEPTH_TRACK_BOTTOM);

        // The curve track's mid-axis rule (200 m of 400 m) is where the 200 m band starts...
        const rules = Array.from(curves.querySelectorAll('line')).map((l) => num(l, 'y1'));
        const sst = screen.getByLabelText('SST 200-250 m');
        expect(rules).toContain(num(sst, 'y'));
        // ...and where the curve's own 200 m sample is plotted.
        expect(curves.querySelector('path')?.getAttribute('d')).toContain(`,${num(sst, 'y').toFixed(1)}`);
    });

    it('the multi-log keeps its geometry: 16 px of top room, plot height as given, rules at quarter depths', () => {
        const { container } = render(
            <DownholeMultiLog
                tracks={[{ label: 'GAMMA', color: '#fff', min: 0, max: 10, points: [{ depth: 0, value: 1 }] }]}
                depthMax={600}
                height={360}
            />,
        );
        const svg = container.querySelector('svg')!;
        expect(num(svg, 'height')).toBe(380);
        const plot = svg.querySelector('rect')!;
        expect(num(plot, 'y')).toBe(16);
        expect(num(plot, 'height')).toBe(360);
        expect(Array.from(svg.querySelectorAll('line')).map((l) => num(l, 'y1'))).toEqual([16, 106, 196, 286, 376]);
        expect(screen.getByText('450')).toBeInTheDocument();
    });

    it('fits the hole to its own data, rounded to a friendly tick, when no axis is given', () => {
        render(<LithologyStripColumn intervals={[band(0, 100), band(100, 130, 'SST')]} holeId="H" height={300} />);
        // 130 m rounds up to a 150 m axis: 30 m is a fifth of the plot.
        const sst = screen.getByLabelText('SST 100-130 m');
        expect(num(sst, 'height')).toBe(60);
        expect(num(sst, 'y')).toBe(DEPTH_TRACK_TOP + (100 / 150) * 300);
    });

    it('treats a depthMax of 0 as "fit the data"', () => {
        render(<LithologyStripColumn intervals={[band(0, 130)]} holeId="H" depthMax={0} height={300} />);
        expect(num(screen.getByLabelText('GRN 0-130 m'), 'height')).toBe(260);
    });

    it('never cuts the bottom off a log: a depthMax shallower than the data is raised to it', () => {
        render(<LithologyStripColumn intervals={[band(0, 130)]} holeId="H" depthMax={100} height={300} />);
        const rect = screen.getByLabelText('GRN 0-130 m');
        expect(num(rect, 'y') + num(rect, 'height')).toBeLessThanOrEqual(DEPTH_TRACK_TOP + 300 + 0.001);
    });
});
