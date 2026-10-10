/**
 * DrillholeStereonet - the per-hole net must be drawn in the units the stored
 * `stereonet_x/y` are in (GIS audit 2026-10, finding 2).
 *
 * promote_silver_to_gold stores the equal-area pole normalised so the
 * primitive circle is at radius 1: the pole of a horizontal bed (dip 0) is at
 * the centre and the pole of a vertical plane (dip 90) is on the rim. The page
 * drew the primitive at sqrt(2), so a vertical structure's pole stopped short
 * of the circle.
 */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import DrillholeStereonet, { PRIMITIVE_RADIUS } from '../DrillholeStereonet';

afterEach(cleanup);

describe('DrillholeStereonet', () => {
    it('draws the primitive circle at the radius the stored poles are normalised to', () => {
        render(<DrillholeStereonet points={[]} />);
        expect(PRIMITIVE_RADIUS).toBe(1);
        expect(screen.getByTestId('stereonet-primitive').getAttribute('r')).toBe('1');
    });

    it('puts the pole of a vertical plane (dip 90, stored radius 1) exactly on the rim', () => {
        render(<DrillholeStereonet points={[{ structure_type: 'fault', stereonet_x: -1, stereonet_y: 0 }]} />);
        const pole = screen.getByTestId('stereonet-pole');
        const r = Math.hypot(Number(pole.getAttribute('cx')), Number(pole.getAttribute('cy')));
        expect(r).toBeCloseTo(Number(screen.getByTestId('stereonet-primitive').getAttribute('r')), 9);
    });

    it('puts the pole of a horizontal bed (dip 0, stored 0,0) at the centre', () => {
        render(<DrillholeStereonet points={[{ structure_type: 'bedding', stereonet_x: 0, stereonet_y: 0 }]} />);
        const pole = screen.getByTestId('stereonet-pole');
        expect(Number(pole.getAttribute('cx'))).toBe(0);
        // SVG y is down and the stored y is north-up; compare magnitudes so -0 is not a failure.
        expect(Math.abs(Number(pole.getAttribute('cy')))).toBe(0);
    });

    it('flips y so north is up', () => {
        render(<DrillholeStereonet points={[{ structure_type: 'joint', stereonet_x: 0, stereonet_y: 0.5 }]} />);
        expect(Number(screen.getByTestId('stereonet-pole').getAttribute('cy'))).toBe(-0.5);
    });

    it('does not draw a measurement that has no orientation (never at the centre)', () => {
        render(
            <DrillholeStereonet
                points={[
                    { structure_type: 'joint', stereonet_x: null, stereonet_y: null },
                    { structure_type: 'joint', stereonet_x: 0.3, stereonet_y: 0.1 },
                ]}
            />,
        );
        expect(screen.getAllByTestId('stereonet-pole')).toHaveLength(1);
    });

    it('keeps the whole primitive circle inside the viewBox', () => {
        render(<DrillholeStereonet points={[]} />);
        const [x, y, w, h] = (screen.getByTestId('drillhole-stereonet').getAttribute('viewBox') ?? '')
            .split(' ')
            .map(Number);
        expect(x).toBeLessThan(-PRIMITIVE_RADIUS);
        expect(y).toBeLessThan(-PRIMITIVE_RADIUS);
        expect(x + w).toBeGreaterThan(PRIMITIVE_RADIUS);
        expect(y + h).toBeGreaterThan(PRIMITIVE_RADIUS);
    });
});
